"""
Canonical constants for the Two Fifteen API.

Mirrors the things we learned empirically during Phase 1 smoke testing
that were either wrong or missing in 215's published OpenAPI spec. Keep
string literals centralised here so the rest of the package doesn't
re-guess them.

Spec: https://www.twofifteen.co.uk/api/openapi.yml
"""

from __future__ import annotations

BASE_URL = "https://www.twofifteen.co.uk/api"

# --- Channel enum ----------------------------------------------------------
# /orders.php POST rejects anything outside this set with HTTP 400.

CHANNEL_SITE = "site"
CHANNEL_API = "API"
CHANNEL_CSV = "csv"
CHANNEL_SHOPIFY = "Shopify"
CHANNEL_WOOCOMMERCE = "WooCommerce"
CHANNEL_EKM = "EKM"
CHANNEL_ETSY = "Etsy"
CHANNEL_TIKTOK = "TikTok"

CHANNELS = {
    CHANNEL_SITE,
    CHANNEL_API,
    CHANNEL_CSV,
    CHANNEL_SHOPIFY,
    CHANNEL_WOOCOMMERCE,
    CHANNEL_EKM,
    CHANNEL_ETSY,
    CHANNEL_TIKTOK,
}

# --- Status integer codes --------------------------------------------------
# Used by GET /orders.php?status=N and GET /orders/count.php?status=N.

STATUS_CREATED = 0
STATUS_PROCESSING_PAYMENT = 1
STATUS_PAID = 2
STATUS_SHIPPED = 3
STATUS_REFUNDED = 4

# --- Base product codes ----------------------------------------------------
# These are 215's internal "base product" codes, NOT per-design SKUs. Every
# ceramic mug 11oz order references CERMUG-01 and supplies the unique design
# per order via `designs[{title, src}]`. See twofifteen/client.py docstring
# for the full explanation.

SKU_CERAMIC_MUG_11OZ = "CERMUG-01"

# --- Canonical decoration titles ------------------------------------------
# When we POST `designs` / `mockups`, 215 normalises our `title` to its own
# canonical name for each product type. Using the canonical values up front
# avoids the round-trip through 215's normaliser.
#
# Ceramic mug 11oz has a single wrap-around decoration position:

DECORATION_CERAMIC_MUG_WRAP = "Printing Front Side"

# --- Address field names ---------------------------------------------------
# 215's actual schema uses camelCase (firstName, lastName, address1, etc).
# Their published OpenAPI summary uses snake_case, which is WRONG. Don't
# trust the snake_case form; always produce these names.

ADDRESS_FIELDS = (
    "firstName",
    "lastName",
    "company",
    "address1",
    "address2",
    "city",
    "county",
    "postcode",
    "country",  # ISO-3166-1 alpha-2 (e.g. "GB")
    "phone1",
    "phone2",
)

# --- Known 215 API quirks --------------------------------------------------
# Record these in code so future debugging has a paper trail.
#
# 1. GET /orders/count.php returns HTTP 500 even on success — body is still
#    valid `{"count": N}` but the status code is broken. `TwoFifteenClient`
#    handles this in list_orders, which is the reliable auth probe.
#
# 2. POST /orders.php returns `{"error": "Name is not defined"}` when the
#    account has no registered brand. Create at least one brand at
#    /my-brands before using the API.
#
# 3. The Order response wraps the payload in {"order": {...}} for single
#    reads and {"orders": [...]} for lists. Creates also return {"order": ...}.
