"""
Phase 2 — Format normalizer.

Walks the picture_dir and card_dir (or any provided directory) and
converts non-JPG source images to JPEG in-place. Designed to run
between `klh match` and `klh mockup` — the matcher flags which files
need conversion, this command handles them.

Handles:
- PNG, WebP, TIFF, BMP, GIF — via Pillow.
- HEIC/HEIF — via pillow-heif (optional dep; we register on import
  and fall back with a clear error if it's not installed).
- EXIF orientation — applied on load so the saved JPEG has pixels
  in the right orientation (no more sideways phone photos).
- Already-JPG files — skipped.
- Non-image files — skipped with a warning.
- Existing .jpg counterpart — the converter will NOT overwrite an
  existing JPG with the same stem. The user is asked to resolve the
  clash manually (usually one is stale and should be deleted).

Usage:
    klh normalize                      # both picture_dir and card_dir
    klh normalize --dry-run            # report only, no file changes
    klh normalize --picture-dir X      # one side only
    klh normalize --card-dir X
    klh normalize --quality 92         # output JPEG quality
    klh normalize --keep-originals     # keep the source files alongside

Exit codes:
    0 — nothing to do, or everything succeeded
    1 — one or more files could not be converted
    2 — configuration error (missing dir, bad arguments)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

from pipeline import config
from pipeline.matcher import CONVERTIBLE_EXTS, JPG_EXTS, IGNORE_NAMES

# Try to enable HEIC/HEIF support. This is optional — if pillow-heif
# isn't installed, HEIC files will fail with a clear message instead
# of a cryptic PIL error.
_HEIF_AVAILABLE = False
try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    _HEIF_AVAILABLE = True
except ImportError:
    pass


DEFAULT_QUALITY = 92


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class NormalizeResult:
    src: Path
    dst: Optional[Path] = None
    status: str = "ok"        # ok | skip | error | dry-run
    message: str = ""


@dataclass
class NormalizeReport:
    scanned_dirs: list[Path] = field(default_factory=list)
    results: list[NormalizeResult] = field(default_factory=list)

    @property
    def n_ok(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def n_skip(self) -> int:
        return sum(1 for r in self.results if r.status == "skip")

    @property
    def n_error(self) -> int:
        return sum(1 for r in self.results if r.status == "error")

    @property
    def n_dry_run(self) -> int:
        return sum(1 for r in self.results if r.status == "dry-run")


# --------------------------------------------------------------------------- #
# Core conversion
# --------------------------------------------------------------------------- #

def _target_path(src: Path) -> Path:
    """Return where `src` should become a JPG."""
    return src.with_suffix(".jpg")


def _convert_one(
    src: Path,
    *,
    quality: int = DEFAULT_QUALITY,
    keep_original: bool = False,
    dry_run: bool = False,
) -> NormalizeResult:
    """Convert a single file to JPG. Pure function over the filesystem."""
    ext = src.suffix.lower()

    if ext in JPG_EXTS:
        return NormalizeResult(src=src, status="skip", message="already JPG")

    if ext not in CONVERTIBLE_EXTS:
        return NormalizeResult(
            src=src, status="skip",
            message=f"unsupported extension {ext}",
        )

    if ext in (".heic", ".heif") and not _HEIF_AVAILABLE:
        return NormalizeResult(
            src=src, status="error",
            message="pillow-heif not installed — run: pip install pillow-heif",
        )

    dst = _target_path(src)
    if dst.exists() and dst.resolve() != src.resolve():
        return NormalizeResult(
            src=src, dst=dst, status="skip",
            message=f"target {dst.name} already exists — resolve manually",
        )

    if dry_run:
        return NormalizeResult(
            src=src, dst=dst, status="dry-run",
            message=f"would convert → {dst.name}",
        )

    try:
        with Image.open(src) as im:
            # Honour any EXIF Orientation tag by actually rotating the pixels
            # so the saved JPG needs no further orientation correction.
            im = ImageOps.exif_transpose(im)

            # JPEGs can't carry alpha — composite onto white as a safe
            # default if the source has transparency.
            if im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            ):
                background = Image.new("RGB", im.size, (255, 255, 255))
                im_rgba = im.convert("RGBA")
                background.paste(im_rgba, mask=im_rgba.split()[-1])
                im = background
            elif im.mode != "RGB":
                im = im.convert("RGB")

            im.save(dst, "JPEG", quality=quality, optimize=True, progressive=True)
    except Exception as e:  # noqa: BLE001 — we want a clean per-file report
        # Clean up a partial destination if we wrote anything.
        if dst.exists() and dst.resolve() != src.resolve():
            try:
                dst.unlink()
            except OSError:
                pass
        return NormalizeResult(src=src, dst=dst, status="error", message=str(e))

    if not keep_original:
        try:
            src.unlink()
        except OSError as e:
            return NormalizeResult(
                src=src, dst=dst, status="error",
                message=f"converted but could not remove original: {e}",
            )

    return NormalizeResult(
        src=src, dst=dst, status="ok",
        message=f"→ {dst.name}",
    )


# --------------------------------------------------------------------------- #
# Directory walk
# --------------------------------------------------------------------------- #

def _scan_convertibles(directory: Path) -> list[Path]:
    """Return every convertible (non-JPG image) file in `directory`."""
    if not directory.exists():
        raise FileNotFoundError(f"Directory not found: {directory}")
    out: list[Path] = []
    for p in sorted(directory.iterdir()):
        if not p.is_file():
            continue
        if p.name in IGNORE_NAMES or p.name.startswith("."):
            continue
        if p.suffix.lower() in CONVERTIBLE_EXTS:
            out.append(p)
    return out


def normalize_dirs(
    dirs: list[Path],
    *,
    quality: int = DEFAULT_QUALITY,
    keep_originals: bool = False,
    dry_run: bool = False,
) -> NormalizeReport:
    """Normalize every convertible file in the given directories."""
    report = NormalizeReport(scanned_dirs=list(dirs))
    for d in dirs:
        for src in _scan_convertibles(d):
            report.results.append(_convert_one(
                src,
                quality=quality,
                keep_original=keep_originals,
                dry_run=dry_run,
            ))
    return report


# --------------------------------------------------------------------------- #
# CLI rendering
# --------------------------------------------------------------------------- #

def _bold(s):   return f"\033[1m{s}\033[0m"
def _green(s):  return f"\033[32m{s}\033[0m"
def _red(s):    return f"\033[31m{s}\033[0m"
def _yellow(s): return f"\033[33m{s}\033[0m"
def _dim(s):    return f"\033[2m{s}\033[0m"


def render_human(report: NormalizeReport, color: bool = True) -> str:
    c = (lambda f, s: f(s)) if color else (lambda f, s: s)
    lines = []
    lines.append(c(_bold, "KLH Normalize"))
    lines.append("=" * 50)
    for d in report.scanned_dirs:
        lines.append(f"Scanned: {d}")
    lines.append("")

    if not report.results:
        lines.append(c(_green, "Nothing to do — no convertible files found."))
        return "\n".join(lines)

    for r in report.results:
        if r.status == "ok":
            lines.append(f"  {c(_green, '✓')} {r.src.name}  {c(_dim, r.message)}")
        elif r.status == "dry-run":
            lines.append(f"  {c(_yellow, '?')} {r.src.name}  {c(_dim, r.message)}")
        elif r.status == "skip":
            lines.append(f"  {c(_dim, '·')} {r.src.name}  {c(_dim, r.message)}")
        elif r.status == "error":
            lines.append(f"  {c(_red, '✗')} {r.src.name}  {c(_red, r.message)}")

    lines.append("")
    summary_parts = []
    if report.n_ok:       summary_parts.append(c(_green, f"{report.n_ok} converted"))
    if report.n_dry_run:  summary_parts.append(c(_yellow, f"{report.n_dry_run} would convert"))
    if report.n_skip:     summary_parts.append(f"{report.n_skip} skipped")
    if report.n_error:    summary_parts.append(c(_red, f"{report.n_error} errors"))
    lines.append(", ".join(summary_parts) or "no changes")

    if not _HEIF_AVAILABLE:
        lines.append("")
        lines.append(c(_dim, "(HEIC/HEIF support not loaded — `pip install pillow-heif` to enable)"))

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI entry
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="klh normalize",
        description="Convert non-JPG sources to JPEG in-place",
    )
    p.add_argument("--picture-dir", type=Path,
                   help="override picture_dir from ~/.klh/config.yaml")
    p.add_argument("--card-dir", type=Path,
                   help="override card_dir from ~/.klh/config.yaml")
    p.add_argument("--only", choices=("picture", "card"),
                   help="restrict to one side only")
    p.add_argument("--quality", type=int, default=DEFAULT_QUALITY,
                   help=f"JPEG quality (default {DEFAULT_QUALITY})")
    p.add_argument("--keep-originals", action="store_true",
                   help="don't delete the source file after conversion")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be done without touching files")
    p.add_argument("--no-color", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = config.load()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    picture_dir = args.picture_dir or cfg.paths.picture_dir
    card_dir = args.card_dir or cfg.paths.card_dir

    dirs: list[Path] = []
    if args.only == "picture":
        dirs = [picture_dir]
    elif args.only == "card":
        dirs = [card_dir]
    else:
        dirs = [picture_dir, card_dir]

    try:
        report = normalize_dirs(
            dirs,
            quality=args.quality,
            keep_originals=args.keep_originals,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(render_human(report, color=not args.no_color))
    return 1 if report.n_error else 0


if __name__ == "__main__":
    sys.exit(main())
