#!/usr/bin/env python3
"""
Phase A: refresh all existing Signed White Card listings with a
clean composite on the new Kim Ruler template.

Dry-run ONLY. No eBay writes happen here. Phase B (separate script)
handles the live ReviseFixedPriceItem step after you've inspected
the outputs.

What it does
------------
1. Walks Kim's active listings via the Trading API.
2. Keeps only those whose Storefront.StoreCategoryID is
   85843959013 (Signed White Cards).
3. Downloads the primary PictureURL for each.
4. Extracts the white card from Nicky's old mockup (threshold +
   largest-bright-region bbox).
5. Resamples the extracted card from its original pixel density up
   to 300 DPI so ruler_composite.py treats it correctly.
6. Picks the best-fit ruler (5x3 / 3x5 / 6x4 / 4x6 / …).
7. Composites the card onto that ruler.
8. Saves the new mockup + indexes the whole batch.

Usage
-----
    # Small smoke test — process 5 listings only
    python scripts/refresh_white_cards.py --limit 5

    # Full run
    python scripts/refresh_white_cards.py

    # Resume after a crash (skips item_ids already in refresh_index.csv)
    python scripts/refresh_white_cards.py --resume

Outputs land in ~/KLH_refresh_dryrun/ by default. Override with --out.
"""

from __future__ import annotations

import argparse
import csv
import sys
import traceback
from pathlib import Path
from urllib.request import urlretrieve

from PIL import Image

# Ensure repo root on sys.path when run as a script.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ebay_api.trading import get_my_ebay_selling, get_item  # noqa: E402
from pipeline.ruler_composite import (                        # noqa: E402
    load_rulers,
    pick_ruler,
    composite_on_ruler,
    DPI as TARGET_DPI,
)

STORE_CATEGORY_WHITE_CARDS = "85843959013"
DEFAULT_OUT_DIR = Path.home() / "KLH_refresh_dryrun"

# Brightness threshold. Nicky's old background is a pale blue-grey
# (~L≈220 in our samples). The card is off-white (~L≈245-255). A
# threshold at 235 reliably separates card from background.
CARD_THRESHOLD = 235

# Nicky's old template renders at roughly this pixel density. Measured
# off our calibration sample (1600px wide, ruler spans ~6.7 inches →
# ~239 px/in). Pass --px-per-inch to override if outputs look wrong.
DEFAULT_OLD_PX_PER_IN = 239


# --------------------------------------------------------------------------- #

def extract_card(img: Image.Image, threshold: int = CARD_THRESHOLD) -> Image.Image:
    """Return the tightest crop around the bright card region.

    Threshold the grayscale image at `threshold`, take the bounding box
    of the resulting bright pixels, and crop. Works because Nicky's
    backgrounds are strictly darker than the white card.
    """
    gray = img.convert("L")
    mask = gray.point(lambda p: 255 if p >= threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        raise ValueError("no bright region detected (empty threshold mask)")

    # Pad a few pixels so we don't clip the card's edge.
    l, t, r, b = bbox
    pad = 4
    l = max(0, l - pad)
    t = max(0, t - pad)
    r = min(img.width, r + pad)
    b = min(img.height, b + pad)
    cropped = img.crop((l, t, r, b))

    # Sanity check: if the crop is weirdly small, something's wrong.
    # A real card should occupy at least 10% of the image width.
    if cropped.width < img.width * 0.10 or cropped.height < img.height * 0.10:
        raise ValueError(
            f"extracted region too small ({cropped.size}) — likely bad source"
        )
    return cropped


def resample_to_300dpi(card: Image.Image, old_px_per_in: float) -> Image.Image:
    """Resize a card from `old_px_per_in` to 300 DPI so downstream ruler
    compositing renders it at its true real-world size."""
    content_w_in = card.width / old_px_per_in
    content_h_in = card.height / old_px_per_in
    new_w = int(round(content_w_in * TARGET_DPI))
    new_h = int(round(content_h_in * TARGET_DPI))
    return card.resize((new_w, new_h), Image.LANCZOS)


# --------------------------------------------------------------------------- #

def iter_white_card_listings(page_limit: int = 100):
    """Yield (item_id, title, picture_urls) for every active white-card
    listing, fetching one page of ActiveList at a time."""
    for page in range(1, page_limit + 1):
        print(f"[page {page}] fetching summary...", file=sys.stderr)
        resp = get_my_ebay_selling(entries_per_page=200, page_number=page)
        items = resp.get("items") or []
        if not items:
            return
        for summary in items:
            item_id = summary["item_id"]
            title = summary.get("title") or ""
            # Need GetItem to see Storefront.StoreCategoryID — not on summary.
            try:
                full = get_item(item_id, include_description=False)
            except Exception as e:
                yield (item_id, title, None, f"GetItem failed: {e}")
                continue
            sf = full.get("Storefront") or {}
            if str(sf.get("StoreCategoryID") or "") != STORE_CATEGORY_WHITE_CARDS:
                continue
            urls = (full.get("PictureDetails") or {}).get("PictureURL") or []
            if isinstance(urls, str):
                urls = [urls]
            yield (item_id, title, urls, None)
        if page >= resp.get("total_pages", 0):
            return


# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR,
                        help=f"output dir (default {DEFAULT_OUT_DIR})")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after processing N listings (default: all)")
    parser.add_argument("--resume", action="store_true",
                        help="skip item_ids already in refresh_index.csv")
    parser.add_argument("--px-per-inch", type=float, default=DEFAULT_OLD_PX_PER_IN,
                        help=f"pixel density of Nicky's old template (default {DEFAULT_OLD_PX_PER_IN})")
    parser.add_argument("--threshold", type=int, default=CARD_THRESHOLD,
                        help=f"card extraction threshold (default {CARD_THRESHOLD})")
    args = parser.parse_args()

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    downloads_dir = out / "downloads"
    extracted_dir = out / "extracted"
    new_dir = out / "new"
    for d in (downloads_dir, extracted_dir, new_dir):
        d.mkdir(exist_ok=True)

    index_path = out / "refresh_index.csv"
    skipped_path = out / "refresh_skipped.csv"

    already = set()
    if args.resume and index_path.exists():
        with open(index_path) as f:
            already = {row["item_id"] for row in csv.DictReader(f)}
        print(f"[resume] skipping {len(already)} already-processed items", file=sys.stderr)

    idx_new = not index_path.exists()
    skip_new = not skipped_path.exists()
    with open(index_path, "a", newline="") as idx_f, \
         open(skipped_path, "a", newline="") as skip_f:

        idx = csv.writer(idx_f)
        skip = csv.writer(skip_f)
        if idx_new:
            idx.writerow([
                "item_id", "title", "old_url", "new_path",
                "ruler", "card_w_px", "card_h_px", "card_w_in", "card_h_in",
            ])
        if skip_new:
            skip.writerow(["item_id", "title", "reason"])

        processed = 0
        rulers = load_rulers()

        for item_id, title, urls, err in iter_white_card_listings():
            if err:
                skip.writerow([item_id, title, err])
                continue
            if item_id in already:
                continue
            if not urls:
                skip.writerow([item_id, title, "no PictureURL"])
                continue

            old_url = urls[0]
            dl_path = downloads_dir / f"{item_id}.jpg"
            try:
                urlretrieve(old_url, dl_path)
            except Exception as e:
                skip.writerow([item_id, title, f"download failed: {e}"])
                continue

            try:
                img = Image.open(dl_path).convert("RGB")
                card = extract_card(img, threshold=args.threshold)
            except Exception as e:
                skip.writerow([item_id, title, f"extract failed: {e}"])
                continue

            card_w_in = card.width / args.px_per_inch
            card_h_in = card.height / args.px_per_inch

            card_300 = resample_to_300dpi(card, args.px_per_inch)
            card_300_path = extracted_dir / f"{item_id}.jpg"
            card_300.save(card_300_path, "JPEG", quality=92)

            ruler = pick_ruler(card_w_in, card_h_in, rulers)
            if ruler is None:
                skip.writerow([item_id, title, "no ruler found"])
                continue

            try:
                out_img = composite_on_ruler(card_300_path, ruler)
            except Exception as e:
                skip.writerow([item_id, title, f"composite failed: {e}\n{traceback.format_exc()}"])
                continue

            new_path = new_dir / f"{item_id}_new.jpg"
            out_img.save(new_path, "JPEG", quality=90, optimize=True)

            idx.writerow([
                item_id, title, old_url, str(new_path),
                ruler.name, card.width, card.height,
                f"{card_w_in:.2f}", f"{card_h_in:.2f}",
            ])
            processed += 1

            if processed % 5 == 0:
                idx_f.flush()
                skip_f.flush()
                print(f"  ✓ {processed} processed", file=sys.stderr)

            if args.limit and processed >= args.limit:
                print(f"[limit reached at {args.limit}]", file=sys.stderr)
                break

    print()
    print(f"Done. {processed} listings processed.")
    print(f"  Outputs:    {new_dir}")
    print(f"  Index CSV:  {index_path}")
    print(f"  Skipped:    {skipped_path}")


if __name__ == "__main__":
    main()
