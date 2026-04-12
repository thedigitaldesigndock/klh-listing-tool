"""
Best Offer auto-decision table.

Kim accepts best offers on every listing £15.99 and up. She auto-
declines anything below a price-dependent minimum and auto-accepts
anything at or above a price-dependent acceptance threshold. This
module holds the lookup table and the formula that generates it.

Curve (agreed with Peter on 2026-04-10)
---------------------------------------
Linear discount scaling from 25% off at £15.99 down to 15% off at
£999.99, rounded to the nearest whole pound, with `min_offer`
defined as `auto_accept - £0.01` (so Kim never sees a best offer she
could have auto-accepted).

Listings below £14.99 are fixed-price only — no Best Offer at all.

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

# Below this price, no Best Offer — fixed price only.
NO_OFFER_THRESHOLD = Decimal("14.99")

# Curve endpoints.
CURVE_START_PRICE = Decimal("15.99")   # first price that has BO enabled
CURVE_END_PRICE   = Decimal("999.99")
CURVE_START_DISCOUNT = Decimal("0.25")  # 25% off at £15.99
CURVE_END_DISCOUNT   = Decimal("0.15")  # 15% off at £999.99

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
    min_offer: float         # auto-decline below this
    auto_accept: float       # auto-accept at or above this

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.list_price, self.min_offer, self.auto_accept)


# --------------------------------------------------------------------------- #
# Curve — formula
# --------------------------------------------------------------------------- #

def _discount_for(price: Decimal) -> Decimal:
    """
    Linear discount from CURVE_START_DISCOUNT at CURVE_START_PRICE to
    CURVE_END_DISCOUNT at CURVE_END_PRICE. Clamped at the endpoints.
    """
    if price <= CURVE_START_PRICE:
        return CURVE_START_DISCOUNT
    if price >= CURVE_END_PRICE:
        return CURVE_END_DISCOUNT
    span = CURVE_END_PRICE - CURVE_START_PRICE
    progress = (price - CURVE_START_PRICE) / span
    return CURVE_START_DISCOUNT - progress * (CURVE_START_DISCOUNT - CURVE_END_DISCOUNT)


def _row_for(price: Decimal) -> Optional[OfferRow]:
    """Compute a single row via the formula. Returns None below the BO threshold."""
    if price < CURVE_START_PRICE:
        return None
    d = _discount_for(price)
    raw_accept = price * (Decimal("1") - d)
    # Round to the nearest whole £
    accept = raw_accept.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    min_offer = accept - Decimal("0.01")
    return OfferRow(
        list_price=float(price),
        min_offer=float(min_offer),
        auto_accept=float(accept),
    )


def generate_table() -> list[Optional[OfferRow]]:
    """Generate the full `.99` table from TABLE_START to TABLE_END."""
    rows: list[Optional[OfferRow]] = []
    p = TABLE_START
    while p <= TABLE_END:
        if p < CURVE_START_PRICE:
            rows.append(None)  # no-BO row — list_price only
        else:
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
# Curve: linear 25% off at £15.99 → 15% off at £999.99, rounded to
# whole £, with min_offer = auto_accept - £0.01. Listings at £14.99 or
# below are fixed-price only (no Best Offer). See pipeline/offers.py
# for the formula and constants.
#
# Schema:
#   offer_table:
#     - [list_price, min_offer, auto_accept]      # Best Offer enabled
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
        if min_o is None or accept is None:
            out[round(list_price, 2)] = None
        else:
            out[round(list_price, 2)] = OfferRow(
                list_price=list_price,
                min_offer=float(min_o),
                auto_accept=float(accept),
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
        f'<BestOfferAutoAcceptPrice currencyID="{currency}">'
        f"{row.auto_accept:.2f}"
        f"</BestOfferAutoAcceptPrice>"
        f'<MinimumBestOfferPrice currencyID="{currency}">'
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
