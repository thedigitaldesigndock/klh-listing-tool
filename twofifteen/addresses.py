"""
Address presets used by the POD smoke test harness and CLI.

These are test addresses for end-to-end verification only. Real eBay
buyer addresses will flow through from the eBay order notification path
in Phase 10c — never from this file.

Fields use 215's camelCase schema (see twofifteen.schema.ADDRESS_FIELDS).
"""

from __future__ import annotations

from typing import Any

# Kim's real shipping address — used for smoke tests and any manual
# orders Kim / Peter want to send themselves. Verified working via the
# end-to-end test on order 613281 on 2026-04-11.
KIM_COWGILL: dict[str, Any] = {
    "firstName": "Kim",
    "lastName":  "Cowgill",
    "company":   "KLH Autographs",
    "address1":  "137 Dobb Brow Road",
    "address2":  "Westhoughton",
    "city":      "Bolton",
    "county":    "",
    "postcode":  "BL5 2BA",
    "country":   "GB",
    "phone1":    "07746137657",
}

PRESETS: dict[str, dict[str, Any]] = {
    "kim": KIM_COWGILL,
}


def get(name: str) -> dict[str, Any]:
    """Fetch a preset by name. Raises KeyError with a helpful message."""
    try:
        return dict(PRESETS[name])
    except KeyError:
        available = ", ".join(sorted(PRESETS)) or "(none)"
        raise KeyError(
            f"unknown address preset '{name}' — available: {available}"
        ) from None
