"""
Phase 1 — Picture / Card matcher.

Reads the picture and card inbox directories, pairs by exact stem,
reports unmatched items, and for each unmatched item suggests the
closest name on the other side via Levenshtein distance. Also flags
non-JPG files that need format normalization before mockup.

Pure read by default. With --fix, prompts to rename files based on
the Levenshtein suggestions.

Usage:
    klh match
    klh match --json
    klh match --fix
    klh match --picture-dir X --card-dir Y
"""

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import Levenshtein

from pipeline import config

# Image formats we recognise. JPG is the target; others need normalization.
JPG_EXTS = {".jpg", ".jpeg"}
CONVERTIBLE_EXTS = {".png", ".webp", ".heic", ".heif", ".tiff", ".tif", ".bmp", ".gif"}
IMAGE_EXTS = JPG_EXTS | CONVERTIBLE_EXTS

# Files we skip entirely.
IGNORE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

# Fuzzy match threshold. A candidate must be within this Levenshtein
# distance to be suggested as a "likely typo".
MAX_TYPO_DISTANCE = 5


@dataclass
class ImageFile:
    path: Path
    stem: str  # filename without extension
    ext: str   # extension including leading dot, lowercased
    is_jpg: bool
    is_convertible: bool
    is_unknown: bool


@dataclass
class Suggestion:
    """A fuzzy-match suggestion: rename `src` to `target_stem` to create a pair."""
    src: Path
    side: str              # "picture" or "card"
    suggested_stem: str
    distance: int


@dataclass
class MatchReport:
    picture_dir: Path
    card_dir: Path
    pictures: list[ImageFile] = field(default_factory=list)
    cards: list[ImageFile] = field(default_factory=list)
    matched_stems: list[str] = field(default_factory=list)
    unmatched_pictures: list[ImageFile] = field(default_factory=list)
    unmatched_cards: list[ImageFile] = field(default_factory=list)
    needs_normalize: list[ImageFile] = field(default_factory=list)
    unknown_format: list[ImageFile] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)

    @property
    def total(self) -> int:
        return max(len(self.pictures), len(self.cards))

    @property
    def all_ok(self) -> bool:
        return (
            not self.unmatched_pictures
            and not self.unmatched_cards
            and not self.needs_normalize
            and not self.unknown_format
        )


def _scan(directory: Path) -> list[ImageFile]:
    """List image-ish files in a directory, skipping junk."""
    if not directory.exists():
        raise FileNotFoundError(f"Directory not found: {directory}")
    files: list[ImageFile] = []
    for p in sorted(directory.iterdir()):
        if not p.is_file():
            continue
        if p.name in IGNORE_NAMES or p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        files.append(
            ImageFile(
                path=p,
                stem=p.stem,
                ext=ext,
                is_jpg=ext in JPG_EXTS,
                is_convertible=ext in CONVERTIBLE_EXTS,
                is_unknown=ext not in IMAGE_EXTS,
            )
        )
    return files


def _closest(target: str, candidates: list[str]) -> tuple[Optional[str], int]:
    """Return the closest candidate to target, plus its Levenshtein distance."""
    if not candidates:
        return None, -1
    best = min(candidates, key=lambda c: Levenshtein.distance(target, c))
    return best, Levenshtein.distance(target, best)


def match(picture_dir: Path, card_dir: Path) -> MatchReport:
    """
    Run the full matcher. Returns a MatchReport with everything the
    caller needs to render output or drive a --fix flow.
    """
    report = MatchReport(picture_dir=picture_dir, card_dir=card_dir)
    report.pictures = _scan(picture_dir)
    report.cards = _scan(card_dir)

    # Index by stem for pairing. An unknown-format file is still matchable
    # by name — the issue is just format, not pairing.
    pics_by_stem: dict[str, ImageFile] = {}
    for f in report.pictures:
        if f.is_unknown:
            report.unknown_format.append(f)
            continue
        if f.is_convertible:
            report.needs_normalize.append(f)
        pics_by_stem.setdefault(f.stem, f)

    cards_by_stem: dict[str, ImageFile] = {}
    for f in report.cards:
        if f.is_unknown:
            report.unknown_format.append(f)
            continue
        if f.is_convertible:
            report.needs_normalize.append(f)
        cards_by_stem.setdefault(f.stem, f)

    pic_stems = set(pics_by_stem)
    card_stems = set(cards_by_stem)

    # Exact-stem pairing.
    matched = pic_stems & card_stems
    report.matched_stems = sorted(matched)

    # Unmatched on each side.
    for stem in sorted(pic_stems - matched):
        report.unmatched_pictures.append(pics_by_stem[stem])
    for stem in sorted(card_stems - matched):
        report.unmatched_cards.append(cards_by_stem[stem])

    # Typo suggestions — look for close matches on the OTHER side.
    unmatched_pic_stems = [f.stem for f in report.unmatched_pictures]
    unmatched_card_stems = [f.stem for f in report.unmatched_cards]

    for f in report.unmatched_pictures:
        best, dist = _closest(f.stem, unmatched_card_stems)
        if best is not None and 0 < dist <= MAX_TYPO_DISTANCE:
            report.suggestions.append(
                Suggestion(src=f.path, side="picture", suggested_stem=best, distance=dist)
            )

    for f in report.unmatched_cards:
        best, dist = _closest(f.stem, unmatched_pic_stems)
        if best is not None and 0 < dist <= MAX_TYPO_DISTANCE:
            report.suggestions.append(
                Suggestion(src=f.path, side="card", suggested_stem=best, distance=dist)
            )

    return report


# ─── Rendering ─────────────────────────────────────────────────────────────

def _bold(s):   return f"\033[1m{s}\033[0m"
def _green(s):  return f"\033[32m{s}\033[0m"
def _red(s):    return f"\033[31m{s}\033[0m"
def _yellow(s): return f"\033[33m{s}\033[0m"
def _dim(s):    return f"\033[2m{s}\033[0m"


def render_human(report: MatchReport, color: bool = True) -> str:
    c = (lambda f, s: f(s)) if color else (lambda f, s: s)
    lines = []
    lines.append(c(_bold, "KLH Matcher"))
    lines.append("=" * 50)
    lines.append(f"Picture folder: {report.picture_dir}")
    lines.append(f"Card folder:    {report.card_dir}")
    lines.append("")

    n_pic = len(report.pictures)
    n_card = len(report.cards)
    n_matched = len(report.matched_stems)

    lines.append(f"Pictures: {n_pic} files")
    lines.append(f"Cards:    {n_card} files")
    lines.append("")

    # Score line
    denom = max(n_pic, n_card)
    score = f"{n_matched}/{denom} matched"
    if n_matched == denom and denom > 0:
        lines.append(c(_green, c(_bold, score)))
    else:
        lines.append(c(_yellow, c(_bold, score)))
    lines.append("")

    if report.matched_stems:
        lines.append(c(_bold, "Matched pairs:"))
        for stem in report.matched_stems:
            lines.append(f"  {c(_green, '✓')} {stem}")
        lines.append("")

    if report.unmatched_pictures:
        lines.append(c(_bold, c(_red, "Unmatched pictures:")))
        for f in report.unmatched_pictures:
            lines.append(f"  {c(_red, '✗')} {f.path.name}")
            suggestion = next(
                (s for s in report.suggestions
                 if s.src == f.path and s.side == "picture"),
                None,
            )
            if suggestion:
                lines.append(c(_dim,
                    f"      → suggest rename to match card: "
                    f"'{suggestion.suggested_stem}{f.ext}'  "
                    f"(distance {suggestion.distance})"))
        lines.append("")

    if report.unmatched_cards:
        lines.append(c(_bold, c(_red, "Unmatched cards:")))
        for f in report.unmatched_cards:
            lines.append(f"  {c(_red, '✗')} {f.path.name}")
            suggestion = next(
                (s for s in report.suggestions
                 if s.src == f.path and s.side == "card"),
                None,
            )
            if suggestion:
                lines.append(c(_dim,
                    f"      → suggest rename to match picture: "
                    f"'{suggestion.suggested_stem}{f.ext}'  "
                    f"(distance {suggestion.distance})"))
        lines.append("")

    if report.needs_normalize:
        lines.append(c(_bold, c(_yellow, "Need format normalize (not JPG):")))
        for f in report.needs_normalize:
            side = "picture" if f.path.parent == report.picture_dir else "card"
            lines.append(f"  {c(_yellow, '!')} {side}/{f.path.name}")
        lines.append("")

    if report.unknown_format:
        lines.append(c(_bold, c(_red, "Unknown format (skipped):")))
        for f in report.unknown_format:
            side = "picture" if f.path.parent == report.picture_dir else "card"
            lines.append(f"  {c(_red, '?')} {side}/{f.path.name}")
        lines.append("")

    # Footer
    if report.all_ok and n_pic == n_card and n_pic > 0:
        lines.append(c(_green, c(_bold, "All good. Ready for mockup.")))
    elif report.all_ok:
        lines.append(c(_yellow, "Matched but counts differ — check for missing files."))
    else:
        issues = []
        if report.unmatched_pictures or report.unmatched_cards:
            issues.append(
                f"{len(report.unmatched_pictures) + len(report.unmatched_cards)} unmatched"
            )
        if report.needs_normalize:
            issues.append(f"{len(report.needs_normalize)} need normalize")
        if report.unknown_format:
            issues.append(f"{len(report.unknown_format)} unknown format")
        lines.append(c(_yellow, c(_bold, "Issues: " + ", ".join(issues))))

    return "\n".join(lines)


def render_json(report: MatchReport) -> str:
    def image_to_dict(f: ImageFile):
        return {
            "path": str(f.path),
            "name": f.path.name,
            "stem": f.stem,
            "ext": f.ext,
            "is_jpg": f.is_jpg,
            "is_convertible": f.is_convertible,
        }

    return json.dumps(
        {
            "picture_dir": str(report.picture_dir),
            "card_dir": str(report.card_dir),
            "picture_count": len(report.pictures),
            "card_count": len(report.cards),
            "matched_count": len(report.matched_stems),
            "matched": report.matched_stems,
            "unmatched_pictures": [image_to_dict(f) for f in report.unmatched_pictures],
            "unmatched_cards": [image_to_dict(f) for f in report.unmatched_cards],
            "needs_normalize": [image_to_dict(f) for f in report.needs_normalize],
            "unknown_format": [image_to_dict(f) for f in report.unknown_format],
            "suggestions": [
                {
                    "src": str(s.src),
                    "side": s.side,
                    "suggested_stem": s.suggested_stem,
                    "distance": s.distance,
                }
                for s in report.suggestions
            ],
            "all_ok": report.all_ok,
        },
        indent=2,
    )


# ─── Interactive fix flow ──────────────────────────────────────────────────

def _dedupe_suggestions(suggestions: list[Suggestion]) -> list[Suggestion]:
    """
    When picture X and card Y each suggest renaming to each other
    (distance 1 either way), keep only ONE suggestion — renaming either
    side fixes the pair, renaming both would undo it. Keep the picture-
    side suggestion by convention.
    """
    seen: set = set()
    deduped: list[Suggestion] = []
    for s in suggestions:
        key = frozenset((s.src.stem, s.suggested_stem))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    # Prefer picture-side when both exist (picture wins in iteration order).
    deduped.sort(key=lambda s: (0 if s.side == "picture" else 1))
    # Re-dedupe after the sort rearranged things (shouldn't matter but safe).
    final: list[Suggestion] = []
    seen2: set = set()
    for s in deduped:
        key = frozenset((s.src.stem, s.suggested_stem))
        if key in seen2:
            continue
        seen2.add(key)
        final.append(s)
    return final


def apply_fixes(report: MatchReport) -> int:
    """
    Walk the suggestions and prompt the user for each rename. Returns
    the number of renames actually performed.

    Bidirectional suggestions are collapsed — renaming one side is
    enough to fix the pair. Targets that already exist are skipped.
    """
    suggestions = _dedupe_suggestions(report.suggestions)
    if not suggestions:
        print("No rename suggestions to apply.")
        return 0

    applied = 0
    print(f"\n{len(suggestions)} rename suggestion(s):\n")
    for s in suggestions:
        new_name = f"{s.suggested_stem}{s.src.suffix}"
        new_path = s.src.parent / new_name

        if new_path.exists():
            print(f"  SKIP  target already exists: {new_name}")
            continue

        prompt = (
            f"  Rename {s.side}/{s.src.name}\n"
            f"      →  {s.side}/{new_name}\n"
            f"      (distance {s.distance})  [y/N]: "
        )
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            break
        if answer in ("y", "yes"):
            s.src.rename(new_path)
            applied += 1
            print(f"      renamed.")
        else:
            print(f"      skipped.")

    return applied


# ─── CLI entry ─────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(
        prog="klh match",
        description="Pair Picture/Card folders and report issues",
    )
    p.add_argument("--picture-dir", type=Path,
                   help="override picture_dir from ~/.klh/config.yaml")
    p.add_argument("--card-dir", type=Path,
                   help="override card_dir from ~/.klh/config.yaml")
    p.add_argument("--json", action="store_true",
                   help="machine-readable JSON output")
    p.add_argument("--fix", action="store_true",
                   help="interactively rename files based on typo suggestions")
    p.add_argument("--no-color", action="store_true",
                   help="disable ANSI color in human output")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.picture_dir and args.card_dir:
        picture_dir = args.picture_dir
        card_dir = args.card_dir
    else:
        cfg = config.load()
        picture_dir = args.picture_dir or cfg.paths.picture_dir
        card_dir = args.card_dir or cfg.paths.card_dir

    try:
        report = match(picture_dir, card_dir)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(render_json(report))
    else:
        print(render_human(report, color=not args.no_color))

    if args.fix:
        applied = apply_fixes(report)
        if applied:
            print(f"\nApplied {applied} rename(s). Re-running match...\n")
            report = match(picture_dir, card_dir)
            print(render_human(report, color=not args.no_color))

    return 0 if report.all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
