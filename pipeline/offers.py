"""
Best Offer auto-decision table.

Kim accepts best offers on every listing £15.99 and up. She auto-
declines anything below a price-dependent minimum and auto-accepts
anything at or above a price-dependent acceptance threshold. This
module holds the lookup table and the formula that generates it.

Table source: Kim's handwritten sheets (photographed 2026-10-23)
----------------------------------------------------------------
Her sheet rows are fixed anchors (KIM_SHEET below). Prices between two
anchors get an auto-accept interpolated linearly between them, rounded
to the nearest whole pound, with `min_offer = auto_accept - £0.01`, so
every offer is either auto-accepted or auto-declined and none ever lands
with Kim to decide (Pete, 23 Oct 2026). From £199.99 up the auto-accept
is 15% off (£199.99 -> £170, matching her £169.99 floor; £299.99 -> £255).

This replaced the April 2026 formula (linear 25% off at £15.99 → 15% off
at £999.99), which was ~24% off at every price and did not match Kim's
sheets (e.g. £79.99 auto-accepted £61 where Kim uses £70).

Listings at £14.99 and below are fixed-price only — no Best Offer at all
(her sheet's £14.99 row is kept only as an interpolation anchor).

Every `.99` price point from £14.99 to £999.99 has an explicit row
in `presets/offers.yaml`. The YAML file is generated from the formula
by `python -m pipeline.offers --regenerate` and committed. Looking
prices up against a persisted table (rather than re-running the
formula every call) gives us:

  * A hard error for non-`.99` prices (no listing accidentally lists
    at a price Kim hasn't signed off on).
  * An auditable table Kim can eyeball like her handwritten sheets.

Public API
----------
    load_offer_table()                 → dict[float, Row|None]
    lookup(price_gbp)                  → Row or None
    has_best_offer(price_gbp)          → bool
    build_best_offer_xml(row, currency)→ str   (Trading API block)

Run as a module:

    python -m pipeline.offers --print        # dump table
    python -m pipeline.offers --regenerate   # rewrite offers.yaml
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

import yaml


# --------------------------------------------------------------------------- #
# Config constants — tweak here to shift the curve
# --------------------------------------------------------------------------- #

# Kim's sheet: list price -> auto-accept (min_offer is always accept - £0.01).
KIM_SHEET: dict[Decimal, Decimal] = {Decimal(p): Decimal(a) for p, a in [
    ("14.99", "10"),  ("16.99", "12"),  ("17.99", "13"),  ("19.99", "15"),
    ("22.99", "16"),  ("24.99", "19"),  ("27.99", "20"),  ("29.99", "22"),
    ("34.99", "25"),  ("39.99", "30"),  ("49.99", "40"),  ("54.99", "45"),
    ("59.99", "50"),  ("64.99", "55"),  ("69.99", "60"),  ("74.99", "65"),
    ("79.99", "70"),  ("84.99", "75"),  ("89.99", "75"),  ("99.99", "85"),
    ("109.99", "90"), ("119.99", "100"), ("124.99", "100"), ("129.99", "110"),
    ("139.99", "115"), ("149.99", "120"), ("169.99", "140"), ("179.99", "150"),
    ("189.99", "160"),
]}

# At or below this price: fixed price, no Best Offer (Pete, 23 Oct 2026).
NO_OFFER_AT_OR_BELOW = Decimal("14.99")

# From this price up, auto-accept at a flat discount (her £199.99 row: £170).
TOP_BAND_FROM     = Decimal("199.99")
TOP_BAND_DISCOUNT = Decimal("0.15")

# Table spans every .99 price in £1 steps.
TABLE_START = Decimal("14.99")
TABLE_END   = Decimal("999.99")
TABLE_STEP  = Decimal("1.00")

# Where the persisted table lives.
OFFERS_YAML = Path(__file__).resolve().parent.parent / "presets" / "offers.yaml"


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OfferRow:
    """One row of the auto-decision table."""
    list_price: float
    min_offer: float                   # auto-decline below this
    auto_accept: Optional[float]       # auto-accept at or above this; None = never auto-accept

    def as_tuple(self) -> tuple[float, float, Optional[float]]:
        return (self.list_price, self.min_offer, self.auto_accept)


# --------------------------------------------------------------------------- #
# Kim's sheet -> full table
# --------------------------------------------------------------------------- #

def _accept_for(price: Decimal) -> Decimal:
    """Auto-accept for `price`: Kim's figure on her rows, linear between them."""
    if price in KIM_SHEET:
        return KIM_SHEET[price]
    if price >= TOP_BAND_FROM:
        return (price * (Decimal("1") - TOP_BAND_DISCOUNT)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    # The £199.99 top-band value closes the last interpolation span.
    anchors = sorted({**KIM_SHEET, TOP_BAND_FROM: _accept_for(TOP_BAND_FROM)}.items())
    lo = max((p, a) for p, a in anchors if p < price)
    hi = min((p, a) for p, a in anchors if p > price)
    raw = lo[1] + (hi[1] - lo[1]) * (price - lo[0]) / (hi[0] - lo[0])
    return raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _row_for(price: Decimal) -> Optional[OfferRow]:
    """One table row. None at or below NO_OFFER_AT_OR_BELOW (no Best Offer)."""
    if price <= NO_OFFER_AT_OR_BELOW:
        return None
    accept = _accept_for(price)
    return OfferRow(
        list_price=float(price),
        min_offer=float(accept - Decimal("0.01")),
        auto_accept=float(accept),
    )


def generate_table() -> list[Optional[OfferRow]]:
    """Generate the full `.99` table from TABLE_START to TABLE_END."""
    rows: list[Optional[OfferRow]] = []
    p = TABLE_START
    while p <= TABLE_END:
        rows.append(_row_for(p))
        p += TABLE_STEP
    return rows


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

_HEADER = """\
# Best Offer auto-decision table.
#
# GENERATED FILE — do NOT hand-edit. Regenerate via:
#   python -m pipeline.offers --regenerate
#
# Source: Kim's handwritten Best Offer sheets (KIM_SHEET in
# pipeline/offers.py). Her rows are exact; prices between them are
# interpolated, rounded to whole £, min_offer = auto_accept - £0.01, so
# every offer is auto-accepted or auto-declined. £199.99 and up: 15% off.
# £14.99 and below: fixed price, no Best Offer.
#
# Schema:
#   offer_table:
#     - [list_price, min_offer, auto_accept]      # Best Offer enabled
#     - [list_price, min_offer, null]             # decline-only, Kim decides the rest
#     - [list_price, null,      null]             # Fixed price only
#
"""


def write_offers_yaml(path: Path = OFFERS_YAML) -> Path:
    """Write the generated table to `presets/offers.yaml`."""
    rows = generate_table()
    lines = [_HEADER, "offer_table:\n"]
    p = TABLE_START
    for row in rows:
        if row is None:
            lines.append(f"  - [{float(p):.2f}, null, null]\n")
        elif row.auto_accept is None:
            lines.append(f"  - [{row.list_price:.2f}, {row.min_offer:.2f}, null]\n")
        else:
            lines.append(
                f"  - [{row.list_price:.2f}, {row.min_offer:.2f}, "
                f"{row.auto_accept:.2f}]\n"
            )
        p += TABLE_STEP
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines))
    return path


def load_offer_table(path: Path = OFFERS_YAML) -> dict[float, Optional[OfferRow]]:
    """
    Load the persisted `presets/offers.yaml` into a dict keyed by
    list_price. Values are OfferRow or None (for no-BO prices).
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing. Run: python -m pipeline.offers --regenerate"
        )
    data = yaml.safe_load(path.read_text())
    table = data.get("offer_table") if isinstance(data, dict) else None
    if not isinstance(table, list):
        raise ValueError(f"{path} has no `offer_table:` list")

    out: dict[float, Optional[OfferRow]] = {}
    for entry in table:
        if not isinstance(entry, list) or len(entry) != 3:
            raise ValueError(f"Bad row in {path}: {entry!r}")
        list_price, min_o, accept = entry
        list_price = float(list_price)
        if min_o is None:
            out[round(list_price, 2)] = None
        else:
            out[round(list_price, 2)] = OfferRow(
                list_price=list_price,
                min_offer=float(min_o),
                auto_accept=float(accept) if accept is not None else None,
            )
    return out


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #

class OfferLookupError(RuntimeError):
    pass


_TABLE_CACHE: Optional[dict[float, Optional[OfferRow]]] = None


def _cached_table() -> dict[float, Optional[OfferRow]]:
    global _TABLE_CACHE
    if _TABLE_CACHE is None:
        _TABLE_CACHE = load_offer_table()
    return _TABLE_CACHE


def lookup(price_gbp: float) -> Optional[OfferRow]:
    """
    Return the OfferRow for an exact `.99` list price.

    Raises OfferLookupError if the price isn't in the table — this is
    intentional: every listing must land on a price Kim has signed
    off on, so a typo like 61.50 fails fast instead of silently
    listing at an un-approved point.
    """
    key = round(float(price_gbp), 2)
    table = _cached_table()
    if key not in table:
        raise OfferLookupError(
            f"No offer-table entry for £{price_gbp:.2f}. "
            f"Valid prices are every .99 from £14.99 to £999.99. "
            f"If this price is legitimate, regenerate the table."
        )
    return table[key]


def has_best_offer(price_gbp: float) -> bool:
    """True if Best Offer is enabled for this list price."""
    return lookup(price_gbp) is not None


# --------------------------------------------------------------------------- #
# Trading API XML
# --------------------------------------------------------------------------- #

def build_best_offer_xml(row: Optional[OfferRow], currency: str = "GBP") -> str:
    """
    Emit the `<BestOfferDetails>` + `<ListingDetails>` snippet for
    AddFixedPriceItem. Returns an empty string if `row is None`
    (no BO on this listing).

    Elements emitted:
        <BestOfferDetails>
          <BestOfferEnabled>true</BestOfferEnabled>
        </BestOfferDetails>
        <ListingDetails>
          <BestOfferAutoAcceptPrice currencyID="GBP">30.00</BestOfferAutoAcceptPrice>
          <MinimumBestOfferPrice    currencyID="GBP">29.99</MinimumBestOfferPrice>
        </ListingDetails>
    """
    if row is None:
        # No-BO listing → omit the block entirely (cheaper than
        # explicitly setting BestOfferEnabled=false, and eBay's default
        # for fixed-price items is already disabled).
        return ""
    return (
        "<BestOfferDetails>"
        "<BestOfferEnabled>true</BestOfferEnabled>"
        "</BestOfferDetails>"
        "<ListingDetails>"
        + (f'<BestOfferAutoAcceptPrice currencyID="{currency}">'
           f"{row.auto_accept:.2f}</BestOfferAutoAcceptPrice>"
           if row.auto_accept is not None else "")
        + f'<MinimumBestOfferPrice currencyID="{currency}">'
        f"{row.min_offer:.2f}"
        f"</MinimumBestOfferPrice>"
        "</ListingDetails>"
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _print_table() -> None:
    table = load_offer_table()
    print(f"{'list':>8}  {'min':>8}  {'accept':>8}  {'discount':>10}")
    print("-" * 42)
    for price, row in sorted(table.items()):
        if row is None:
            print(f"  £{price:6.2f}  {'—':>8}  {'—':>8}  {'fixed':>10}")
        elif row.auto_accept is None:
            print(f"  £{row.list_price:6.2f}  £{row.min_offer:6.2f}  {'Kim':>8}  {'decides':>10}")
        else:
            disc = (row.list_price - row.auto_accept) / row.list_price * 100
            print(
                f"  £{row.list_price:6.2f}  "
                f"£{row.min_offer:6.2f}  "
                f"£{row.auto_accept:6.2f}  "
                f"{disc:8.1f}%"
            )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.offers",
        description="Print or regenerate the Best Offer auto-decision table",
    )
    parser.add_argument("--print", action="store_true",
                        help="dump the current table to stdout")
    parser.add_argument("--regenerate", action="store_true",
                        help="rewrite presets/offers.yaml from the formula")
    parser.add_argument("--lookup", type=float, default=None,
                        help="show the row for a single price")
    args = parser.parse_args(argv)

    if args.regenerate:
        path = write_offers_yaml()
        # Invalidate cache so subsequent lookups see the new file.
        global _TABLE_CACHE
        _TABLE_CACHE = None
        print(f"✓ wrote {path} ({sum(1 for _ in generate_table())} rows)")

    if args.lookup is not None:
        row = lookup(args.lookup)
        if row is None:
            print(f"£{args.lookup:.2f}: fixed price (no Best Offer)")
        else:
            print(f"£{row.list_price:.2f}: "
                  f"min £{row.min_offer:.2f}, "
                  f"auto-accept £{row.auto_accept:.2f}")
        return 0

    if args.print or (not args.regenerate and args.lookup is None):
        _print_table()

    return 0


if __name__ == "__main__":
    sys.exit(main())
