#!/usr/bin/env python3
"""
Daily catch-up sweep for listings created on Nicky's PC (or anywhere)
that didn't get extras attached or were silently dropped to football.

Two passes — both safe to re-run, both idempotent:

  PASS 1 — Extras:
    For every active listing whose product type has extras configured,
    GetItem, count current pictures, and if fewer than expected,
    upload the missing extras and ReviseFixedPriceItem.

  PASS 2 — Re-cat:
    For every active listing whose signer is in signer_genre.yaml as a
    non-football genre but the listing currently lives in a football
    category, move it to the right cat per category_map.yaml. Skip if
    already in any "OK cat" for that genre family.

Scope is "started in the last 7 days" by default — tunable via --days.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from ebay_api import trading
from ebay_api.trading import trading_call
from dashboard.workflow import _get_extra_image_paths, _cached_upload, _PICTURE_URL_CACHE
from pipeline import audit_db, presets as pp, config as pcfg


NS = '{urn:ebay:apis:eBLBaseComponents}'
REPO_ROOT = Path(__file__).resolve().parent.parent

# Title pattern → product key for the extras lookup. Keep in sync with
# the actual products.yaml keys.
TITLE_TO_PROD = [
    (re.compile(r"A4 Framed Photo Display", re.I),    "a4_frame_a"),
    (re.compile(r"A4 Photo Mount Display",  re.I),     "a4_mount_a"),
    (re.compile(r"10x8 Framed Photo Display", re.I),   "10x8_frame"),
    (re.compile(r"10x8 Photo Mount Display", re.I),    "10x8_mount"),
    (re.compile(r"16x12 Framed Photo Display", re.I),  "16x12_frame_a"),
    (re.compile(r"16x12 (?:Mount|Photo Mount)", re.I), "16x12_mount_a"),
    (re.compile(r"\bSigned 6x4 Photo\b", re.I),        "photo_6x4"),
    (re.compile(r"\bSigned 10x8 Photo\b", re.I),       "photo_10x8"),
    (re.compile(r"\bSigned 12x8 Photo\b", re.I),       "photo_12x8"),
    (re.compile(r"\bSigned Card\b", re.I),             "odd_card"),
    # Catchall: any "Hand Signed Photo" (or "Signed Photo") without a
    # size keyword — that's the "Odd Size Photo" dashboard button. Routes
    # to the odd_photo product which uses "All Other Products" extras
    # (Kim's Picture 2 helper image). Listed last so explicit sizes above
    # win first.
    (re.compile(r"\b(?:Hand\s+)?Signed Photo\b", re.I), "odd_photo"),
]

# Category families per genre — listings in any of these for a given
# genre are "fine, leave alone". Used for the re-cat pass to avoid
# downgrading specificity (e.g. Music:Pop:Autographs → Music:Other).
GENRE_OK_CATS: dict[str, set[str]] = {
    "music":              {"2328", "178898", "91484"},
    "tv_male":            {"35030", "69536", "2312"},
    "tv_female":          {"35031", "69536", "2312"},
    "film_male":          {"35027", "2312"},
    "film_female":        {"35028", "2312"},
    "sport_cricket":      {"6087", "2853"},
    "sport_tennis":       {"430"},
    "sport_golf":         {"1223"},
    "sport_f1":           {"2835"},
    "sport_basketball":   {"2826"},
    "sport_cycling":      {"2855"},
    "sport_rugby_league": {"68369"},
    "sport_rugby_union":  {"2881"},
    "sport_boxing":       {"550"},
}
FOOTBALL_CATS = {"27290", "97085", "2841", "27289", "2885"}

# ConditionID per target cat. Music cats need 1000 (New); most need
# 3000 (Used) for autographs. eBay rejects unknown ConditionIDs.
COND_BY_CAT: dict[str, str] = {
    "2328": "1000", "178898": "1000", "91484": "1000",
    # everything else defaults to 3000 (Used)
}


def _signer_from_title(title: str) -> str | None:
    """First 2 words (or 3 with honorific) — same rule as stream_recat_is."""
    m = re.match(r"^(?:Sir|Dame|Lady|Lord|Dr|Prof|Rev) [A-Z][\w\-'.]+ [A-Z][\w\-'.]+",
                 title, re.I)
    if m:
        return m.group(0).lower()
    m = re.match(r"^([A-Z][\w\-'.]+ [A-Z][\w\-'.]+)", title)
    return m.group(1).lower() if m else None


def _detect_product(title: str) -> str | None:
    for pat, key in TITLE_TO_PROD:
        if pat.search(title):
            return key
    return None


def _pull_recent_listings(days: int) -> list[dict]:
    """All active listings started in the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    for page in range(1, 6):
        inner = (f"<ActiveList><Sort>StartTimeDescending</Sort>"
                 f"<Pagination><EntriesPerPage>200</EntriesPerPage>"
                 f"<PageNumber>{page}</PageNumber></Pagination></ActiveList>")
        root = trading_call("GetMyeBaySelling", inner)
        items = root.findall(f'.//{NS}ActiveList/{NS}ItemArray/{NS}Item')
        if not items:
            break
        added_any = False
        for it in items:
            start_text = it.find(f'.//{NS}ListingDetails/{NS}StartTime').text
            start_dt = datetime.strptime(start_text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            if start_dt < cutoff:
                # Pages are sorted DESC; once we cross the cutoff, stop.
                return rows
            rows.append({
                "iid":   it.find(f'{NS}ItemID').text,
                "title": it.find(f'{NS}Title').text or "",
                "start": start_text,
            })
            added_any = True
        if not added_any:
            break
    return rows


def _pass_1_extras(listings: list[dict]) -> tuple[int, int, int]:
    """Returns (fixed, skipped, failed)."""
    bundle = pp.load()
    cfg = pcfg.load()
    _PICTURE_URL_CACHE.clear()
    fixed = skipped = failed = 0
    print(f"\n=== Pass 1: extras ===  scanning {len(listings)} listings")

    for item in listings:
        prod_key = _detect_product(item["title"])
        if not prod_key:
            skipped += 1
            continue
        extras = _get_extra_image_paths(cfg.paths.extra_images_dir, prod_key, bundle)
        if not extras:
            skipped += 1
            continue

        try:
            raw = trading.get_item(item["iid"], include_description=False)
        except Exception as e:
            print(f"  [{item['iid']}] GetItem fail: {str(e)[:100]}")
            failed += 1
            continue

        pics = raw.get("PictureDetails", {}).get("PictureURL", [])
        if isinstance(pics, str):
            pics = [pics]
        if len(pics) >= 1 + len(extras):
            skipped += 1
            continue

        try:
            new_pics = list(pics) + [_cached_upload(p) for p in extras]
            xml = ("<Item>" + f"<ItemID>{item['iid']}</ItemID>" +
                   "<PictureDetails><GalleryType>Gallery</GalleryType>" +
                   "".join(f"<PictureURL>{u}</PictureURL>" for u in new_pics) +
                   "</PictureDetails></Item>")
            r = trading_call("ReviseFixedPriceItem", xml)
            ack = r.find(f'{NS}Ack').text
            print(f"  [{item['iid']}] {prod_key} {len(pics)}→{len(new_pics)} ack={ack}")
            fixed += 1
        except Exception as e:
            print(f"  [{item['iid']}] FAIL {prod_key}: {str(e)[:120]}")
            failed += 1

    return fixed, skipped, failed


def _pass_2_recat(listings: list[dict]) -> tuple[int, int, int]:
    """Returns (fixed, skipped, failed)."""
    g = yaml.safe_load((REPO_ROOT / "presets" / "signer_genre.yaml").open())
    signers = {k.lower(): v for k, v in (g.get("signers") or {}).items()}
    cm = yaml.safe_load((REPO_ROOT / "presets" / "category_map.yaml").open())
    genre_to_cat = {gn: m.get("Photo", {}).get("id")
                    for gn, m in (cm.get("map") or {}).items()}

    fixed = skipped = failed = 0
    print(f"\n=== Pass 2: re-cat ===  scanning {len(listings)} listings")

    for item in listings:
        signer = _signer_from_title(item["title"])
        if not signer or signer not in signers:
            skipped += 1
            continue
        genre = signers[signer]
        if genre.startswith("football"):
            skipped += 1
            continue
        target = genre_to_cat.get(genre)
        if not target or target == "??":
            skipped += 1
            continue

        try:
            raw = trading.get_item(item["iid"], include_description=False)
        except Exception as e:
            print(f"  [{item['iid']}] GetItem fail: {str(e)[:100]}")
            failed += 1
            continue
        cur_cat = raw.get("PrimaryCategory", {}).get("CategoryID")
        if cur_cat in GENRE_OK_CATS.get(genre, set()):
            skipped += 1
            continue
        if cur_cat not in FOOTBALL_CATS:
            skipped += 1   # not in football, don't presume to move
            continue

        cond = COND_BY_CAT.get(target, "3000")
        xml = (f"<Item><ItemID>{item['iid']}</ItemID>"
               f"<PrimaryCategory><CategoryID>{target}</CategoryID></PrimaryCategory>"
               f"<ConditionID>{cond}</ConditionID></Item>")
        try:
            r = trading_call("ReviseFixedPriceItem", xml)
            ack = r.find(f'{NS}Ack').text
            print(f"  [{item['iid']}] {cur_cat} → {target} ({genre}) cond={cond} ack={ack}")
            fixed += 1
        except Exception as e:
            print(f"  [{item['iid']}] FAIL {target}: {str(e)[:120]}")
            failed += 1

    return fixed, skipped, failed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7,
                   help="Look at listings started in the last N days (default 7).")
    p.add_argument("--apply", action="store_true",
                   help="(no-op flag for shell symmetry — always live).")
    args = p.parse_args()

    listings = _pull_recent_listings(args.days)
    print(f"Pulled {len(listings)} listings from the last {args.days} days")

    e_fix, e_skip, e_fail = _pass_1_extras(listings)
    c_fix, c_skip, c_fail = _pass_2_recat(listings)

    summary = (f"extras_fixed={e_fix} extras_skipped={e_skip} extras_failed={e_fail} "
               f"recat_fixed={c_fix} recat_skipped={c_skip} recat_failed={c_fail}")
    print(f"\n=== Summary ===\n  {summary}")

    with audit_db.connect() as conn:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
            ("DAILY_LISTING_REPAIR", now, summary),
        )
        conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
