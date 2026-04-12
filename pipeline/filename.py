"""
Filename metadata parser.

Kim's workflow has Nicky naming each scan using a structured stem:

    <Name>_<Field1>_<Category>_<Variant>.<ext>

    e.g.  Wayne Rooney_Man Utd_Football_1.jpg
          Harry Kane_Spurs_Football.jpg          (no variant)
          Ellis Genge_Leicester Tigers_Rugby_2.jpg
          Ronnie O'Sullivan_Rocket_Snooker.jpg

Pricing is NOT in the filename. The dashboard captures per-listing
prices after mockup, when Nicky can see the preview thumbnail and
decide. This keeps the scan-time job lean — just name the file.

The primary scan goes in the ONE/ folder and the optional secondary
scan (signed card OR second photo, depending on product type) goes
in the TWO/ folder under the same filename. The matcher pairs them
by `pair_key` (which is just the full stem, since no price is stripped).

Legacy stems that still carry a trailing `_99.99` price tag will
parse cleanly too — the price falls into `ParsedFilename.price` as
a fallback but is no longer the canonical source.

Rules
-----
- Field 0 (Name) is required. Everything else is optional.
- Price is detected as a trailing `^\\d+\\.\\d{2}$` segment, for
  backward-compat with any legacy files still floating around.
  New files shouldn't carry it.
- Variant is detected as a trailing pure-integer segment AFTER any
  price stripping. Any digits-only segment counts: "1", "02", "10".
- Empty middle fields are OK. `Seamus Coleman__Football.jpg` is legal:
  name="Seamus Coleman", field1=None, category="Football".
- Whitespace is trimmed; empty strings become None.
- No validation of Field1 / Category against the knowledge base
  happens here — that's the caller's job (pipeline.presets).

The parser is deliberately forgiving: any stem that doesn't follow the
convention still produces a ParsedFilename with just `name` populated,
and the listing pipeline can fall back to command-line flags.

Pair key
--------
`ParsedFilename.pair_key` is everything-except-price. Two files with
the same `pair_key` are the ONE + TWO pair for the same listing. With
the new no-price filename convention, pair_key is effectively the
full stem; the stripping logic is kept only for legacy compatibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


# Trailing price like "99.99", "149.99", "9.99" — always two decimals.
_PRICE_RE = re.compile(r"^\d+\.\d{2}$")
# Variant is a pure-integer segment: "1", "02", "10".
_VARIANT_RE = re.compile(r"^\d+$")


@dataclass
class ParsedFilename:
    """What we pulled out of a picture filename stem."""
    name: str                       # signer (field 0) — always present
    field1: Optional[str] = None    # club / band / show / nickname (field 1)
    category: Optional[str] = None  # Football / Music / Rugby / TV … (field 2)
    variant: Optional[str] = None   # integer tag disambiguating multiple
                                    # cards for the same signer (field 3)
    price: Optional[float] = None   # trailing .99 price if any

    def is_empty(self) -> bool:
        return (
            not self.name
            and self.field1 is None
            and self.category is None
            and self.variant is None
        )

    @property
    def pair_key(self) -> str:
        """
        Everything-except-price, underscore-joined. Two files sharing a
        pair_key are the picture + card for the same listing.

        >>> parse_stem("Wayne Rooney_Man Utd_Football_1_49.99").pair_key
        'Wayne Rooney_Man Utd_Football_1'
        >>> parse_stem("Wayne Rooney_Man Utd_Football_1").pair_key
        'Wayne Rooney_Man Utd_Football_1'
        """
        parts: list[str] = [self.name]
        if self.field1 is not None:
            parts.append(self.field1)
        if self.category is not None:
            parts.append(self.category)
        if self.variant is not None:
            parts.append(self.variant)
        return "_".join(parts)

    def describe(self) -> str:
        """One-line human-readable summary, for CLI logging."""
        bits = [f"name={self.name!r}"]
        if self.field1:
            bits.append(f"field1={self.field1!r}")
        if self.category:
            bits.append(f"category={self.category!r}")
        if self.variant:
            bits.append(f"variant={self.variant!r}")
        if self.price is not None:
            bits.append(f"price=£{self.price:.2f}")
        return " ".join(bits)


def _nonempty(s: Optional[str]) -> Optional[str]:
    """Trim whitespace; return None for empty string."""
    if s is None:
        return None
    s = s.strip()
    return s or None


def parse_stem(stem: str) -> ParsedFilename:
    """
    Split a picture stem on underscores and pull out name / field1 /
    category / variant / price fields. Never raises — returns a
    best-effort ParsedFilename.

    Parsing order (outside-in, last segment first):
      1. If the trailing segment looks like a price (^\\d+\\.\\d{2}$),
         pop it as `price`.
      2. If the new trailing segment is a pure integer, pop it as
         `variant`.
      3. Remaining segments map to name / field1 / category.

    >>> parse_stem("Wayne Rooney_Man Utd_Football_1_49.99")
    ParsedFilename(name='Wayne Rooney', field1='Man Utd',
                   category='Football', variant='1', price=49.99)

    >>> parse_stem("Wayne Rooney_Man Utd_Football_1")       # card file
    ParsedFilename(name='Wayne Rooney', field1='Man Utd',
                   category='Football', variant='1', price=None)

    >>> parse_stem("Seamus Coleman_Everton_Football_99.99")  # no variant
    ParsedFilename(name='Seamus Coleman', field1='Everton',
                   category='Football', variant=None, price=99.99)

    >>> parse_stem("Seamus Coleman")
    ParsedFilename(name='Seamus Coleman')
    """
    if not stem:
        return ParsedFilename(name="")

    parts = [p.strip() for p in stem.split("_")]

    # 1. Trailing price?
    price: Optional[float] = None
    if parts and _PRICE_RE.match(parts[-1] or ""):
        try:
            price = float(parts[-1])
        except ValueError:
            price = None
        else:
            parts = parts[:-1]

    # 2. Trailing variant? (pure integer, only meaningful if there's at
    #    least a name segment in front of it — we never consume parts[0]
    #    as a variant even if Nicky names a file "7.jpg".)
    variant: Optional[str] = None
    if len(parts) >= 2 and _VARIANT_RE.match(parts[-1] or ""):
        variant = parts[-1]
        parts = parts[:-1]

    name = parts[0] if parts else ""
    field1 = _nonempty(parts[1]) if len(parts) > 1 else None
    category = _nonempty(parts[2]) if len(parts) > 2 else None

    return ParsedFilename(
        name=name.strip(),
        field1=field1,
        category=category,
        variant=variant,
        price=price,
    )


def parse_path(path: Union[str, Path]) -> ParsedFilename:
    """Parse from a filesystem path — convenience wrapper."""
    return parse_stem(Path(path).stem)


def merge_with_flags(
    parsed: ParsedFilename,
    *,
    name: Optional[str] = None,
    qualifier: Optional[str] = None,
    category: Optional[str] = None,
    variant: Optional[str] = None,
    price: Optional[float] = None,
) -> ParsedFilename:
    """
    Merge an in-memory ParsedFilename with explicit CLI flag values.

    Any non-None flag wins over the parsed stem value — so Nicky's
    filename sets the defaults and Kim (or a CLI user) can override
    any of them without renaming the file. Returns a new ParsedFilename.
    """
    return ParsedFilename(
        name=(name.strip() if name else parsed.name),
        field1=(qualifier.strip() if qualifier else parsed.field1),
        category=(category.strip() if category else parsed.category),
        variant=(variant if variant is not None else parsed.variant),
        price=(price if price is not None else parsed.price),
    )
