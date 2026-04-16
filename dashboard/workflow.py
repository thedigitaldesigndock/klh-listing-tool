"""
Dashboard workflow endpoints — the three buttons Nicky presses.

This module registers:

    GET  /api/match                — scan ONE/ + TWO/ and return a
                                     MatchReport-shaped JSON blob
    POST /api/mockup               — render a mockup for one pair_key +
                                     product_key, stash it under
                                     mockups_dir, and return a URL
    GET  /api/mockup-image/{name}  — serve a rendered mockup back to the
                                     browser (bounded inside mockups_dir)
    POST /api/list                 — verify / schedule / submit a listing
                                     via pipeline.lister, with a hard
                                     default of dry-run (verify) unless
                                     the caller explicitly opts in

Design notes
------------
* Every endpoint re-reads ~/.klh/config.yaml on each call via
  pipeline.config.load() so Nicky can edit paths without a server
  reboot. Cheap (a few dozen lines of YAML).
* The match endpoint returns a flat, JSON-safe version of
  pipeline.matcher.MatchReport — the dataclass has Path objects in it
  which FastAPI can't serialize out of the box.
* The mockup endpoint runs pipeline.compositor synchronously. Each
  render is ~500ms so we don't need a task queue for single-item flows.
  If we ever want batch-render-all we'll switch to a background task.
* The list endpoint is the only one that can touch eBay. It defaults
  to `verify_only=True` (dry run) and refuses to go live unless the
  caller sends `verify_only=false` AND `confirm=true`. Same belt-and-
  braces pattern as the CLI `klh list --confirm`.
* upload_site_hosted_picture / verify_listing / submit_listing /
  schedule_listing are resolved lazily via `pipeline.lister` attribute
  lookup inside the handler. Tests can monkeypatch any of them without
  messing with the app construction.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import io
import zipfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from pipeline import config as pcfg
from pipeline import compositor
from pipeline import ruler_composite
from pipeline import lister
from pipeline import matcher
from pipeline import presets as pp
from pipeline.filename import parse_stem, ParsedFilename


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _safe_path(path: Optional[Path]) -> Optional[str]:
    return str(path) if path else None


def _image_file_to_dict(f: matcher.ImageFile) -> dict:
    """Convert a matcher.ImageFile to a JSON-safe dict."""
    parsed = parse_stem(f.pair_key) if f.pair_key else None
    return {
        "path":           str(f.path),
        "name":           f.path.name,
        "stem":           f.stem,
        "ext":            f.ext,
        "is_jpg":         f.is_jpg,
        "is_convertible": f.is_convertible,
        "is_unknown":     f.is_unknown,
        "pair_key":       f.pair_key,
        "price":          f.price,
        # Parsed stem components so the frontend can render unmatched
        # pictures as valid rows for no-secondary products (photo-only
        # and odd-size card/photo), where TWO/ isn't required.
        "parsed": {
            "name":     parsed.name,
            "field1":   parsed.field1,
            "category": parsed.category,
            "variant":  parsed.variant,
        } if parsed else None,
    }


def _suggestion_to_dict(s: matcher.Suggestion) -> dict:
    return {
        "src":             str(s.src),
        "src_name":        s.src.name,
        "side":            s.side,
        "suggested_stem":  s.suggested_stem,
        "distance":        s.distance,
    }


def _report_to_dict(report: matcher.MatchReport) -> dict:
    """Flatten a MatchReport into a shape the frontend can render."""
    pics_by_key = {f.pair_key: f for f in report.pictures if not f.is_unknown}
    cards_by_key = {f.pair_key: f for f in report.cards if not f.is_unknown}

    matched_entries: list[dict] = []
    for key in report.matched_pair_keys:
        pic = pics_by_key.get(key)
        card = cards_by_key.get(key)
        parsed = parse_stem(key)
        matched_entries.append({
            "pair_key": key,
            "parsed": {
                "name":     parsed.name,
                "field1":   parsed.field1,
                "category": parsed.category,
                "variant":  parsed.variant,
            },
            "picture": _image_file_to_dict(pic) if pic else None,
            "card":    _image_file_to_dict(card) if card else None,
        })

    return {
        "ok":                 report.all_ok,
        "picture_dir":        str(report.picture_dir),
        "card_dir":           str(report.card_dir),
        "totals": {
            "pictures":           len(report.pictures),
            "cards":              len(report.cards),
            "matched":            len(report.matched_pair_keys),
            "unmatched_pictures": len(report.unmatched_pictures),
            "unmatched_cards":    len(report.unmatched_cards),
            "needs_normalize":    len(report.needs_normalize),
            "unknown_format":     len(report.unknown_format),
        },
        "matched":             matched_entries,
        "unmatched_pictures":  [_image_file_to_dict(f) for f in report.unmatched_pictures],
        "unmatched_cards":     [_image_file_to_dict(f) for f in report.unmatched_cards],
        "needs_normalize":     [_image_file_to_dict(f) for f in report.needs_normalize],
        "unknown_format":      [_image_file_to_dict(f) for f in report.unknown_format],
        "suggestions":         [_suggestion_to_dict(s) for s in report.suggestions],
    }


def _find_file_for_pair_key(directory: Path, pair_key: str) -> Optional[Path]:
    """
    Return the JPG/JPEG file in `directory` whose parsed stem matches
    `pair_key`. None if no match. Ignores non-image files.

    We re-scan rather than caching — the ONE/TWO folders are tens of
    files, not thousands, and the rescan is ~1ms.
    """
    if not directory or not directory.exists():
        return None
    for p in sorted(directory.iterdir()):
        if not p.is_file() or p.name in matcher.IGNORE_NAMES:
            continue
        if p.suffix.lower() not in matcher.IMAGE_EXTS:
            continue
        parsed = parse_stem(p.stem)
        if parsed.pair_key == pair_key:
            return p
    return None


def _read_display_size(image_path: Path) -> tuple[int, int]:
    """
    Open an image on disk, apply its EXIF Orientation, and return the
    (width, height) as it would appear in Finder. This is the single
    source of truth for "what shape is this scan?" — used by both the
    orientation and photo-size detectors below.
    """
    from PIL import Image, ImageOps  # local import — already a compositor dep
    with Image.open(image_path) as raw:
        im = ImageOps.exif_transpose(raw)
        return im.size


def _detect_orientation(image_path: Path) -> str:
    """
    Return "landscape" or "portrait" from pixel dimensions.
    Ties break landscape (matches ruler_composite).

    Used for products with `orientation_lock: auto` (10x8 mount/frame,
    16x12 CDEF) so pick_template_id can resolve to the correct -land /
    -port variant without Nicky having to flag it.
    """
    w, h = _read_display_size(image_path)
    return "landscape" if w >= h else "portrait"


def _detect_photo_size(image_path: Path) -> str:
    """
    Return "10x8" or "12x8" based on the scan's long-side / short-side
    aspect ratio.

    10x8 photos are 10÷8 = 1.25, 12x8 photos are 12÷8 = 1.5. Those two
    ratios are far apart (20% difference) so a midpoint threshold at
    1.375 gives robust classification even for slightly cropped or
    padded scans.

    Used for 16x12 CDEF (main_size: auto) which routes to one of four
    templates based on photo_size × orientation:

        12x8 landscape → 16x12-c-mount/frame
        12x8 portrait  → 16x12-d-mount/frame
        10x8 landscape → 16x12-e-mount/frame
        10x8 portrait  → 16x12-f-mount/frame
    """
    w, h = _read_display_size(image_path)
    long_side  = max(w, h)
    short_side = min(w, h)
    ratio = long_side / short_side if short_side else 1.0
    # Midpoint between 1.25 (10x8) and 1.5 (12x8) — anything below this
    # is closer to 10x8, anything above is closer to 12x8.
    return "10x8" if ratio < 1.375 else "12x8"


# --------------------------------------------------------------------------- #
# Extra listing images
# --------------------------------------------------------------------------- #
#
# Kim's stock photos that get appended to every listing after the main
# mockup/scan. Stored in <extra_images_dir>/<group>/ on disk, where
# <group> is the product's `extra_images_group` value from products.yaml.
# Sorted alphabetically so the order is deterministic (Picture 2 before
# Picture 3, etc.).

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _get_extra_image_paths(
    extra_images_dir: Optional[Path],
    product_key: str,
    bundle: pp.PresetsBundle,
) -> list[Path]:
    """
    Return sorted paths for the extra listing images that belong to
    this product, or an empty list if no extras are configured / found.
    """
    if not extra_images_dir or not extra_images_dir.exists():
        return []
    product = bundle.products.get(product_key)
    if not product:
        return []
    group = product.raw.get("extra_images_group")
    if not group:
        return []
    group_dir = extra_images_dir / group
    if not group_dir.is_dir():
        return []
    return sorted(
        p for p in group_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
    )


# --------------------------------------------------------------------------- #
# Picture URL cache
# --------------------------------------------------------------------------- #
#
# eBay's UploadSiteHostedPictures has a brutal daily call cap on sandbox
# (and a non-trivial one on prod). Verify and List both need a hosted
# URL, so without caching a single Verify→List sequence burns two upload
# calls on the same bytes. The cache key is
# (absolute_path, mtime_ns, size) so any real edit to the file invalidates
# it automatically — we never serve a stale URL when Nicky re-renders a
# mockup.
#
# Scope: module-level, lives for the life of the dashboard process. Not
# persisted to disk (deliberate — a server restart forces fresh uploads,
# which is the right behaviour after a crash).

_PICTURE_URL_CACHE: dict[tuple[str, int, int], str] = {}


def _cache_key(path: Path) -> Optional[tuple[str, int, int]]:
    """
    Build the (path, mtime, size) key for the picture URL cache.
    Returns None if the file doesn't exist.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (str(path.resolve()), st.st_mtime_ns, st.st_size)


def _cached_upload(path: Path) -> str:
    """
    Upload `path` to eBay Picture Services, or return a cached URL from
    a previous upload if the same file bytes have already been hosted.

    Any exception from `lister.upload_site_hosted_picture` (including
    rate-limit errors) propagates — the caller wraps it into a 502.
    """
    key = _cache_key(path)
    if key is not None and key in _PICTURE_URL_CACHE:
        return _PICTURE_URL_CACHE[key]
    url = lister.upload_site_hosted_picture(path)
    if key is not None:
        _PICTURE_URL_CACHE[key] = url
    return url


def _mockup_filename(product_key: str, pair_key: str) -> str:
    """Deterministic output filename for a rendered mockup.

    Safe to re-run — overwrites the previous render for the same
    (product_key, pair_key). Uses underscore joining so the whole
    thing is one filesystem-safe token.
    """
    safe_pair = pair_key.replace("/", "_").replace("\\", "_")
    return f"{product_key}__{safe_pair}.jpg"


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #

class MockupRequest(BaseModel):
    product_key: str
    pair_key: str
    orientation: Optional[str] = None          # "landscape" | "portrait" | None
    variant: Optional[str] = None              # explicit variant override
    display_name: Optional[str] = None         # override compositor text


class PreviewRequest(BaseModel):
    """Pure build-listing preview — no eBay, no uploads, no disk IO.

    Used by the dashboard to show the rendered title / specifics BEFORE
    the first Verify round-trip, so Nicky can eyeball and edit the
    title before burning an UploadSiteHostedPictures call.
    """
    product_key: str
    pair_key: str
    price_gbp: float
    orientation: Optional[str] = None
    variant: Optional[str] = None
    subject: Optional[str] = None
    item_specifics: Optional[dict[str, str]] = None


class DownloadMockupsRequest(BaseModel):
    """Zip up rendered mockups for a set of pair_keys."""
    product_key: str
    pair_keys: list[str]


class ListRequest(BaseModel):
    product_key: str
    pair_key: str
    price_gbp: float
    quantity: int = 1                          # duplicates Kim holds of this item
    mockup_filename: Optional[str] = None      # defaults to _mockup_filename()
    orientation: Optional[str] = None
    variant: Optional[str] = None
    subject: Optional[str] = None
    item_specifics: Optional[dict[str, str]] = None
    title_override: Optional[str] = None       # user-edited title from the UI

    # Lister safety
    verify_only: bool = True                   # dry-run by default
    confirm: bool = False                      # required to go live
    schedule_at: Optional[datetime] = None     # ISO8601 → scheduled listing


# --------------------------------------------------------------------------- #
# Route registration
# --------------------------------------------------------------------------- #

def _build_listing_for_request(bundle, req, *, title_override=None):
    """
    Shared build_listing() call used by /api/preview and /api/list.
    Applies the optional title_override via the `overrides` deep-merge
    hook so it gets the same treatment as any other field.
    """
    parsed = parse_stem(req.pair_key)
    overrides = None
    if title_override and title_override.strip():
        overrides = {"title": title_override.strip()}
    return pp.build_listing(
        bundle,
        product_key=req.product_key,
        parsed=parsed,
        subject=req.subject,
        orientation=req.orientation,
        variant=req.variant,
        price_gbp=req.price_gbp,
        # /api/preview's PreviewRequest doesn't carry quantity — it affects
        # the XML Quantity field, not the title/price/best-offer rendering
        # that the preview panel displays — so default to 1 when missing.
        quantity=getattr(req, "quantity", None),
        item_specifics=req.item_specifics,
        overrides=overrides,
    )


def _listing_summary(listing: dict) -> dict:
    """
    Flatten a build_listing() dict into the shape the frontend shows
    in the workflow panel. Used by both /api/preview and /api/list.
    """
    return {
        "title":                listing.get("title"),
        "price_gbp":            listing.get("price_gbp"),
        "category_id":          listing.get("category_id"),
        "template_id":          listing.get("template_id"),
        "best_offer":           listing.get("best_offer"),
        "item_specifics":       listing.get("item_specifics"),
        "item_specifics_count": len(listing.get("item_specifics") or {}),
    }


def register_workflow_routes(app: FastAPI) -> None:
    """Attach /api/match, /api/mockup, /api/mockup-image, /api/preview, /api/list."""

    @app.get("/api/match")
    def api_match() -> JSONResponse:
        """
        Scan the configured ONE/ + TWO/ folders and return a pairing
        report. Returns 500 if ~/.klh/config.yaml is missing or the
        folders don't exist.
        """
        try:
            cfg = pcfg.load()
        except pcfg.ConfigError as e:
            return JSONResponse(
                {"ok": False, "error": str(e)},
                status_code=500,
            )

        picture_dir = cfg.paths.picture_dir
        card_dir    = cfg.paths.card_dir
        if not picture_dir or not picture_dir.exists():
            return JSONResponse(
                {"ok": False, "error": f"picture_dir missing: {picture_dir}"},
                status_code=500,
            )
        if not card_dir or not card_dir.exists():
            return JSONResponse(
                {"ok": False, "error": f"card_dir missing: {card_dir}"},
                status_code=500,
            )

        report = matcher.match(picture_dir, card_dir)
        return JSONResponse(_report_to_dict(report))

    @app.post("/api/mockup")
    def api_mockup(req: MockupRequest, request: Request) -> JSONResponse:
        """
        Render a mockup for one (product_key, pair_key) pair.

        Returns the output filename + a URL the frontend can embed
        (`/api/mockup-image/<filename>`). Photo-only products have no
        template — the endpoint returns ok=true with mockup_url=None
        and the caller should use the raw scan instead.
        """
        bundle: pp.PresetsBundle = request.app.state.bundle

        # Product lookup first — a bad key should fail before we touch disk.
        try:
            product = bundle.product(req.product_key)
        except pp.PresetsError as e:
            raise HTTPException(status_code=404, detail=str(e))

        try:
            cfg = pcfg.load()
        except pcfg.ConfigError as e:
            raise HTTPException(status_code=500, detail=str(e))

        picture_path = _find_file_for_pair_key(cfg.paths.picture_dir, req.pair_key)
        card_path    = _find_file_for_pair_key(cfg.paths.card_dir, req.pair_key)
        if picture_path is None:
            raise HTTPException(
                status_code=404,
                detail=f"no picture in {cfg.paths.picture_dir} matching "
                       f"pair_key={req.pair_key!r}",
            )

        parsed = parse_stem(req.pair_key)

        # Odd-size card / photo: composite onto a Kim Ruler background.
        # The picker chooses the best-fit ruler from templates/rulers/
        # based on the scan's detected content size.
        odd_layouts = {"odd_card", "odd_photo"}
        if product.raw.get("layout") in odd_layouts:
            try:
                img, ruler = ruler_composite.render_odd_size_mockup(picture_path)
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"ruler composite failed: {e}",
                )
            out_name = _mockup_filename(req.product_key, req.pair_key)
            out_path = cfg.paths.mockups_dir / out_name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path, "JPEG", quality=90, optimize=True)
            return JSONResponse({
                "ok":            True,
                "product_key":   req.product_key,
                "template_id":   None,
                "ruler":         ruler.name,
                "mockup_url":    f"/api/mockup-image/{out_name}",
                "mockup_path":   str(out_path),
                "is_raw_photo":  False,
                "parsed": {
                    "name":     parsed.name,
                    "field1":   parsed.field1,
                    "category": parsed.category,
                    "variant":  parsed.variant,
                },
            })

        # Photo-only products (6x4, 10x8, 12x8): no compositor work.
        # Return ok with no URL — the frontend knows to use the raw scan.
        if product.template_id is None:
            return JSONResponse({
                "ok":            True,
                "product_key":   req.product_key,
                "template_id":   None,
                "mockup_url":    None,
                "mockup_path":   str(picture_path),
                "is_raw_photo":  True,
                "parsed": {
                    "name":     parsed.name,
                    "field1":   parsed.field1,
                    "category": parsed.category,
                    "variant":  parsed.variant,
                },
            })

        # Resolve template_id.
        #
        # Two auto-detect hooks, both driven by the product's config:
        #   * orientation_lock: auto → detect landscape/portrait from
        #     the scan's displayed dimensions (10x8, 16x12 CDEF).
        #   * main_size: auto        → detect 10x8 vs 12x8 from the
        #     scan's long/short aspect ratio (16x12 CDEF).
        #
        # Without these, pick_template_id falls back to the product's
        # default template_id (e.g. "10x8-mount" or "16x12-c-mount")
        # which may not be a real folder on disk, so compositor.load_spec
        # 404s and Nicky sees no mockup.
        orientation = req.orientation
        if orientation is None and product.raw.get("orientation_lock") == "auto":
            try:
                orientation = _detect_orientation(picture_path)
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"could not read {picture_path} to detect orientation: {e}",
                )

        photo_size: Optional[str] = None
        if product.raw.get("main_size") == "auto":
            try:
                photo_size = _detect_photo_size(picture_path)
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"could not read {picture_path} to detect photo size: {e}",
                )

        try:
            template_id = pp.pick_template_id(
                bundle,
                req.product_key,
                orientation=orientation,
                photo_size=photo_size,
                variant=req.variant,
            )
        except pp.PresetsError as e:
            raise HTTPException(status_code=400, detail=str(e))

        try:
            spec = compositor.load_spec(template_id)
        except FileNotFoundError as e:
            raise HTTPException(
                status_code=404,
                detail=f"template {template_id!r} has no spec.yaml on disk: {e}",
            )

        display_name = req.display_name or parsed.name or picture_path.stem

        # A4-B templates have a "secondary" slot (PICTURE 2) instead of
        # a "card" slot.  The content of TWO/ doubles as either the card
        # or the secondary photo depending on which slot the template
        # defines.
        secondary_path: Optional[Path] = None
        if "secondary" in spec.slots:
            secondary_path = card_path
            card_path = None  # don't also paste into a non-existent card slot

        try:
            img = compositor.composite(
                spec,
                picture_path=picture_path,
                card_path=card_path,
                name=display_name,
                secondary_path=secondary_path,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"composite failed: {e}")

        out_name = _mockup_filename(req.product_key, req.pair_key)
        out_path = cfg.paths.mockups_dir / out_name
        compositor.save_mockup(img, out_path, spec)

        return JSONResponse({
            "ok":           True,
            "product_key":  req.product_key,
            "template_id":  template_id,
            "mockup_url":   f"/api/mockup-image/{out_name}",
            "mockup_path":  str(out_path),
            "is_raw_photo": False,
            "parsed": {
                "name":     parsed.name,
                "field1":   parsed.field1,
                "category": parsed.category,
                "variant":  parsed.variant,
            },
        })

    @app.get("/api/mockup-image/{filename}")
    def api_mockup_image(filename: str) -> FileResponse:
        """
        Serve a rendered mockup back to the browser.

        Locked to mockups_dir — rejects slashes, dots-only, or anything
        that resolves outside the configured folder. Serves as JPEG
        (the compositor always saves JPEG).
        """
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise HTTPException(status_code=400, detail="invalid filename")

        try:
            cfg = pcfg.load()
        except pcfg.ConfigError as e:
            raise HTTPException(status_code=500, detail=str(e))

        mockups_dir = cfg.paths.mockups_dir
        target = mockups_dir / filename
        try:
            resolved = target.resolve(strict=True)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="mockup not found")
        if mockups_dir.resolve() not in resolved.parents:
            raise HTTPException(status_code=400, detail="invalid filename")
        return FileResponse(resolved, media_type="image/jpeg")

    @app.post("/api/download-mockups")
    def api_download_mockups(req: DownloadMockupsRequest) -> StreamingResponse:
        """
        Zip rendered mockups for the given pair_keys and stream the
        archive back. Files inside the zip use the pair_key as the
        filename (matching the ONE folder naming convention), and the
        zip itself is named after the product.
        """
        try:
            cfg = pcfg.load()
        except pcfg.ConfigError as e:
            raise HTTPException(status_code=500, detail=str(e))

        mockups_dir = cfg.paths.mockups_dir
        buf = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for pair_key in req.pair_keys:
                disk_name = _mockup_filename(req.product_key, pair_key)
                src = mockups_dir / disk_name
                if src.exists():
                    # Inside the zip: just the pair_key stem + .jpg
                    arc_name = pair_key.replace("/", "_").replace("\\", "_") + ".jpg"
                    zf.write(src, arc_name)
                    count += 1

        if count == 0:
            raise HTTPException(
                status_code=404,
                detail="No rendered mockups found for the given pair keys.",
            )

        buf.seek(0)
        zip_name = f"{req.product_key}_mockups.zip"
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{zip_name}"',
            },
        )

    @app.post("/api/preview")
    def api_preview(req: PreviewRequest, request: Request) -> JSONResponse:
        """
        Build the would-be listing dict for a (product, pair_key, price)
        triple and return its title / specifics / best-offer — without
        touching eBay, the compositor, or the filesystem.

        This is the endpoint the dashboard hits right after a scan to
        populate each row's editable title field.
        """
        bundle: pp.PresetsBundle = request.app.state.bundle
        try:
            bundle.product(req.product_key)   # validate key
        except pp.PresetsError as e:
            raise HTTPException(status_code=404, detail=str(e))
        try:
            listing = _build_listing_for_request(bundle, req)
        except pp.PresetsError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"build_listing failed: {e}")
        return JSONResponse({
            "ok":      True,
            "summary": _listing_summary(listing),
        })

    @app.post("/api/list")
    def api_list(req: ListRequest, request: Request) -> JSONResponse:
        """
        Verify / schedule / submit a listing.

        Flow:
          1. Look up the product + parsed filename
          2. presets.build_listing(...) → listing dict
          3. Find the mockup file (product template) OR the raw picture
             (photo-only products)
          4. Upload pictures to eBay Picture Services
          5. verify_listing (dry-run) OR submit/schedule (live)

        Safety:
          * Default verify_only=True — no live listings ever get made
            without a deliberate verify_only=False + confirm=true call.
          * schedule_at is an ISO 8601 datetime; if set, schedule_listing
            is used instead of submit_listing.
          * A missing mockup file raises 404. The caller is expected to
            have hit /api/mockup first.
        """
        bundle: pp.PresetsBundle = request.app.state.bundle

        try:
            product = bundle.product(req.product_key)
        except pp.PresetsError as e:
            raise HTTPException(status_code=404, detail=str(e))

        try:
            cfg = pcfg.load()
        except pcfg.ConfigError as e:
            raise HTTPException(status_code=500, detail=str(e))

        # ---- Build the listing dict ---------------------------------- #
        try:
            listing = _build_listing_for_request(
                bundle, req, title_override=req.title_override
            )
        except pp.PresetsError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # ---- Locate the picture/mockup to upload --------------------- #
        picture_path = _find_file_for_pair_key(
            cfg.paths.picture_dir, req.pair_key
        )
        if picture_path is None:
            raise HTTPException(
                status_code=404,
                detail=f"no picture in {cfg.paths.picture_dir} matching "
                       f"pair_key={req.pair_key!r}",
            )

        # Three upload modes:
        #   1. Template-based products (mounts, frames) → upload the
        #      composited mockup PNG.
        #   2. Odd-size card/photo → upload the ruler-composite mockup
        #      (template_id is None but we still have a generated file).
        #   3. Plain photo-only (6x4/10x8/12x8) → upload the raw scan,
        #      no mockup stage involved.
        odd_layouts = {"odd_card", "odd_photo"}
        layout = product.raw.get("layout")
        needs_mockup = (product.template_id is not None) or (layout in odd_layouts)

        if needs_mockup:
            mockup_name = req.mockup_filename or _mockup_filename(
                req.product_key, req.pair_key
            )
            mockup_path = cfg.paths.mockups_dir / mockup_name
            if not mockup_path.exists():
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"mockup not found at {mockup_path}. "
                        f"Call /api/mockup first."
                    ),
                )
            upload_paths: list[Path] = [mockup_path]
        else:
            # Photo-only: upload the raw scan, no mockup required.
            upload_paths = [picture_path]

        # Append Kim's stock extra images for this product type.
        extra_paths = _get_extra_image_paths(
            cfg.paths.extra_images_dir, req.product_key, bundle
        )
        upload_paths.extend(extra_paths)

        # ---- Safety gates --------------------------------------------- #
        if not req.verify_only and not req.confirm:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Going live requires confirm=true. "
                    "Send verify_only=true for a dry run."
                ),
            )

        # ---- Upload pictures ------------------------------------------ #
        # Uses the in-memory (path, mtime, size) cache so Verify → List
        # on the same row doesn't double-spend eBay's UploadSiteHostedPictures
        # quota. See _cached_upload for details.
        try:
            picture_urls = [_cached_upload(p) for p in upload_paths]
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"picture upload failed: {e}",
            )

        # ---- Run the Trading call ------------------------------------- #
        try:
            if req.verify_only:
                result = lister.verify_listing(listing, picture_urls)
                action = "verify"
            elif req.schedule_at is not None:
                result = lister.schedule_listing(
                    listing,
                    picture_urls,
                    schedule_time=req.schedule_at,
                    confirm=True,
                )
                action = "schedule"
            else:
                result = lister.submit_listing(
                    listing,
                    picture_urls,
                    confirm=True,
                )
                action = "submit"
        except lister.ListerError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"trading call failed: {e}",
            )

        return JSONResponse({
            "ok":           True,
            "action":       action,
            "result":       result,
            "picture_urls": picture_urls,
            "summary":      _listing_summary(listing),
        })
