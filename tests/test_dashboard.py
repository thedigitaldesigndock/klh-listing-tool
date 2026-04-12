"""
Smoke tests for the dashboard FastAPI app and catalog view-model.

These are intentionally shape-oriented — they lock down the contract
the frontend relies on (`/api/products` → tiles[], products{}, etc.)
without pinning exact button labels / prices. Those can drift in
products.yaml without breaking the dashboard JS.

The match/mockup/list endpoints land in a later phase; tests for
those will live alongside this file.
"""

from __future__ import annotations

import pytest

from dashboard import catalog
from pipeline import presets as pp


# Guard: TestClient needs httpx and fastapi installed. If the user has
# skipped the `dashboard` extras, skip the whole module rather than
# spamming import errors.
fastapi = pytest.importorskip("fastapi")
httpx   = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from dashboard.app import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(scope="module")
def bundle() -> pp.PresetsBundle:
    return pp.load()


# --------------------------------------------------------------------------- #
# catalog.build_catalog — pure view-model
# --------------------------------------------------------------------------- #

def test_build_catalog_shape(bundle: pp.PresetsBundle):
    cat = catalog.build_catalog(bundle)
    assert set(cat.keys()) == {
        "tile_groups", "tiles", "products", "layout_twins",
        "orphan_layouts", "total_tiles", "total_products",
    }
    assert cat["total_products"] == 22
    assert cat["total_tiles"] >= 11  # 9 mount/frame + 4 photo-only
    assert cat["orphan_layouts"] == []


def test_build_catalog_toggleable_tiles_have_both_twins(bundle: pp.PresetsBundle):
    cat = catalog.build_catalog(bundle)
    toggleable = [t for t in cat["tiles"] if t["has_toggle"]]
    # 9 layouts: a4_a, a4_b, 10x8, 16x12_a..f
    assert len(toggleable) == 9
    for tile in toggleable:
        assert tile["mount"] is not None
        assert tile["frame"] is not None
        assert tile["mount"]["product_key"] != tile["frame"]["product_key"]


def test_build_catalog_photo_only_tiles_have_no_frame_twin(bundle: pp.PresetsBundle):
    cat = catalog.build_catalog(bundle)
    photo_only = [t for t in cat["tiles"] if not t["has_toggle"]]
    assert {t["layout"] for t in photo_only} == {
        "photo_6x4", "photo_10x8", "photo_12x8", "card_only",
    }
    for tile in photo_only:
        assert tile["frame"] is None
        assert tile["mount"] is not None


def test_build_catalog_respects_dashboard_order(bundle: pp.PresetsBundle):
    cat = catalog.build_catalog(bundle)
    # Flatten the grouped dashboard_order to get expected layout sequence.
    expected_order: list[str] = []
    for entry in bundle.dashboard_order:
        if isinstance(entry, str):
            expected_order.append(entry)
        elif isinstance(entry, dict):
            expected_order.extend(entry.get("layouts") or [])
    actual_order = [t["layout"] for t in cat["tiles"]]
    assert actual_order == expected_order


def test_build_catalog_tile_groups(bundle: pp.PresetsBundle):
    cat = catalog.build_catalog(bundle)
    groups = cat["tile_groups"]
    assert len(groups) == 3
    labels = [g["label"] for g in groups]
    assert labels == ["Other Products", "10x8 / A4", "16x12"]
    # All tiles across groups should equal the flat tiles list.
    flat_from_groups = [t for g in groups for t in g["tiles"]]
    assert len(flat_from_groups) == len(cat["tiles"])


def test_product_view_has_all_frontend_fields(bundle: pp.PresetsBundle):
    cat = catalog.build_catalog(bundle)
    sample = cat["products"]["16x12_mount_a"]
    required = {
        "product_key", "button_label", "layout", "frame",
        "template_id", "preview_url", "main_size", "needs_secondary",
        "orientation_lock", "default_price_gbp",
        "suggested_prices", "title_pattern", "size_clause",
    }
    assert required <= set(sample.keys())
    assert sample["frame"] is False
    assert isinstance(sample["suggested_prices"], list)
    assert sample["default_price_gbp"] in sample["suggested_prices"]


def test_preview_url_set_for_existing_templates(bundle: pp.PresetsBundle):
    """16x12-c-mount has a preview.jpg checked in, so it should resolve."""
    cat = catalog.build_catalog(bundle)
    p = cat["products"]["16x12_mount_c"]
    assert p["preview_url"] == "/api/template-preview/16x12-c-mount"


def test_preview_url_placeholder_for_photo_only_products(bundle: pp.PresetsBundle):
    """Photo-only products have no template but get a static placeholder."""
    cat = catalog.build_catalog(bundle)
    for key in ("photo_6x4", "photo_10x8", "photo_12x8", "card_only"):
        url = cat["products"][key]["preview_url"]
        assert url is not None, f"{key}: should have a placeholder preview_url"
        assert "/static/placeholders/" in url, \
            f"{key}: preview_url should point to static placeholder, got {url}"


# --------------------------------------------------------------------------- #
# HTTP routes
# --------------------------------------------------------------------------- #

def test_health(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_get_products_returns_catalog(client: TestClient):
    r = client.get("/api/products")
    assert r.status_code == 200
    data = r.json()
    assert data["total_products"] == 22
    # 13 tiles total: 9 toggleable + 4 photo-only
    assert data["total_tiles"] == 13
    assert len(data["tiles"]) == 13


def test_index_serves_shell(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert b"KLH Listing Dashboard" in r.content
    # The shell should reference our static bundle.
    assert b"/static/app.js" in r.content
    assert b"/static/style.css" in r.content


def test_static_assets_served(client: TestClient):
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert b"fetchCatalog" in r.content

    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert b".product-grid" in r.content


def test_template_preview_route_serves_jpeg(client: TestClient):
    """Happy path: a real template_id streams its preview.jpg."""
    r = client.get("/api/template-preview/16x12-c-mount")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    # JPEG magic bytes
    assert r.content[:3] == b"\xff\xd8\xff"


def test_template_preview_route_404_for_missing(client: TestClient):
    r = client.get("/api/template-preview/does-not-exist")
    assert r.status_code == 404


def test_template_preview_route_rejects_path_traversal(client: TestClient):
    """A template_id containing a slash must be rejected before disk."""
    # Encoded slash → FastAPI still routes it as a single path param.
    r = client.get("/api/template-preview/..%2Fpresets")
    assert r.status_code in (400, 404)


def test_config_endpoint_shape(client: TestClient):
    """
    Config may or may not have valid ONE/TWO paths on the test box —
    either way the endpoint should return a well-shaped JSON dict.
    """
    r = client.get("/api/config")
    assert r.status_code in (200, 500)
    if r.status_code == 200:
        data = r.json()
        assert "ok" in data
        assert "one" in data and "two" in data
        for slot in ("one", "two"):
            assert "path"   in data[slot]
            assert "exists" in data[slot]
