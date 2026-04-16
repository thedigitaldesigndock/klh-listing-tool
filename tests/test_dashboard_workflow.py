"""
Tests for dashboard.workflow — /api/match, /api/mockup, /api/list.

These run with FastAPI's TestClient. The tricky part is that the
handlers read ~/.klh/config.yaml on every call; we handle that by
writing a real temp config.yaml and monkey-patching the module-level
CONFIG_PATH constant before building the TestClient.

All network and Pillow work is faked:

  * /api/mockup tests monkeypatch `compositor.load_spec` and
    `compositor.composite` / `compositor.save_mockup`, so we don't
    need a real base.png/overlay.png/spec.yaml on disk.
  * /api/list tests monkeypatch lister.upload_site_hosted_picture,
    verify_listing, submit_listing, and schedule_listing, so nothing
    ever touches eBay.

We still exercise the real matcher (matcher.match is pure disk I/O)
and the real presets.build_listing — that's the glue we care about.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
httpx   = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from pipeline import config as pcfg  # noqa: E402
from pipeline import compositor as pcomp  # noqa: E402
from pipeline import lister as plister  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Tiny JPEG helper — writes a valid 4×4 JPG to disk
# --------------------------------------------------------------------------- #

def _write_jpg(path: Path, color=(255, 128, 64)) -> Path:
    """Write a 4×4 single-color JPEG to `path`. Keeps file size tiny."""
    from PIL import Image
    img = Image.new("RGB", (4, 4), color=color)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=80)
    return path


# --------------------------------------------------------------------------- #
# Temp config fixture
# --------------------------------------------------------------------------- #

@pytest.fixture
def klh_config(tmp_path, monkeypatch):
    """
    Build a temp ~/.klh/-style layout with a real config.yaml pointing at
    tmp_path subdirs. Monkeypatches pipeline.config.CONFIG_PATH so every
    call to pcfg.load() reads our synthetic file.

    Yields a dict of paths the tests can drop files into.
    """
    root = tmp_path / "klh_test"
    picture_dir    = root / "ONE"
    card_dir       = root / "TWO"
    products_dir   = root / "Products"
    normalized_dir = root / "normalized"
    mockups_dir    = root / "mockups"
    listed_dir     = root / "listed"
    for d in (picture_dir, card_dir, products_dir,
              normalized_dir, mockups_dir, listed_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Build the YAML content — pcfg._expand calls os.path.expanduser,
    # so ~ would be resolved, but we pass absolute paths anyway.
    config_yaml = (
        "paths:\n"
        f"  picture_dir: {picture_dir}\n"
        f"  card_dir: {card_dir}\n"
        f"  products_dir: {products_dir}\n"
        f"  normalized_dir: {normalized_dir}\n"
        f"  mockups_dir: {mockups_dir}\n"
        f"  listed_dir: {listed_dir}\n"
        f"env_file: {root}/.env\n"
        f"tokens_file: {root}/tokens.json\n"
    )
    (root / "config.yaml").write_text(config_yaml)
    (root / ".env").write_text("EBAY_APP_ID=fake\n")
    (root / "tokens.json").write_text("{}")

    monkeypatch.setattr(pcfg, "CONFIG_PATH", root / "config.yaml")

    return {
        "root":           root,
        "picture_dir":    picture_dir,
        "card_dir":       card_dir,
        "mockups_dir":    mockups_dir,
        "normalized_dir": normalized_dir,
        "listed_dir":     listed_dir,
    }


@pytest.fixture
def client(klh_config):
    """
    Build a TestClient against a fresh create_app().

    `klh_config` is requested first so the CONFIG_PATH monkeypatch is
    live before the app's lifespan runs.
    """
    from dashboard.app import create_app
    return TestClient(create_app())


# --------------------------------------------------------------------------- #
# /api/match
# --------------------------------------------------------------------------- #

def test_match_empty_folders(client, klh_config):
    """Empty ONE/TWO → all zeros, ok flag defined."""
    r = client.get("/api/match")
    assert r.status_code == 200
    data = r.json()
    assert data["totals"]["pictures"] == 0
    assert data["totals"]["cards"] == 0
    assert data["totals"]["matched"] == 0
    assert data["matched"] == []


def test_match_happy_pair(client, klh_config):
    """One picture + one card with the same stem → one matched entry."""
    stem = "Seamus Coleman_Everton_Football"
    _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")
    _write_jpg(klh_config["card_dir"] / f"{stem}.jpg")

    r = client.get("/api/match")
    assert r.status_code == 200
    data = r.json()
    assert data["totals"]["matched"] == 1
    assert data["totals"]["unmatched_pictures"] == 0
    assert data["totals"]["unmatched_cards"] == 0

    matched = data["matched"]
    assert len(matched) == 1
    entry = matched[0]
    assert entry["pair_key"] == stem
    assert entry["parsed"]["name"] == "Seamus Coleman"
    assert entry["parsed"]["field1"] == "Everton"
    assert entry["parsed"]["category"] == "Football"
    assert entry["picture"]["name"] == f"{stem}.jpg"
    assert entry["card"]["name"] == f"{stem}.jpg"


def test_match_unmatched_and_suggestions(client, klh_config):
    """Picture without a card → unmatched list populated."""
    _write_jpg(klh_config["picture_dir"] / "Joe Root_England_Cricket.jpg")
    # No matching card.
    r = client.get("/api/match")
    assert r.status_code == 200
    data = r.json()
    assert data["totals"]["unmatched_pictures"] == 1
    names = [f["name"] for f in data["unmatched_pictures"]]
    assert "Joe Root_England_Cricket.jpg" in names


def test_match_500_when_config_missing(tmp_path, monkeypatch):
    """If the config file is missing, /api/match should 500 with an error."""
    monkeypatch.setattr(pcfg, "CONFIG_PATH", tmp_path / "nope.yaml")
    from dashboard.app import create_app
    client = TestClient(create_app())
    r = client.get("/api/match")
    assert r.status_code == 500
    assert "error" in r.json()


# --------------------------------------------------------------------------- #
# /api/mockup
# --------------------------------------------------------------------------- #

def test_mockup_photo_only_returns_raw(client, klh_config):
    """
    photo_10x8 has template_id=None; the endpoint should return
    mockup_url=None and is_raw_photo=True, pointing at the scan on disk.
    """
    stem = "Mel C_Spice Girls_Music"
    pic_path = _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")

    r = client.post(
        "/api/mockup",
        json={"product_key": "photo_10x8", "pair_key": stem},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["is_raw_photo"] is True
    assert data["mockup_url"] is None
    assert data["mockup_path"] == str(pic_path)
    assert data["template_id"] is None
    assert data["parsed"]["name"] == "Mel C"


def test_mockup_photo_only_unknown_pair_key_404(client, klh_config):
    """No file matches the pair_key → 404."""
    r = client.post(
        "/api/mockup",
        json={"product_key": "photo_10x8", "pair_key": "Nobody_Nowhere_Cricket"},
    )
    assert r.status_code == 404


def test_mockup_unknown_product_404(client, klh_config):
    stem = "Wayne Rooney_Man Utd_Football"
    _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")
    r = client.post(
        "/api/mockup",
        json={"product_key": "not_a_real_product", "pair_key": stem},
    )
    assert r.status_code == 404


def test_mockup_templated_product_uses_compositor(client, klh_config, monkeypatch):
    """
    For a mount/frame product, /api/mockup must call compositor.load_spec
    and compositor.composite, then save under mockups_dir. We monkeypatch
    the compositor so we don't need real templates on disk.
    """
    from PIL import Image

    stem = "Wayne Rooney_Man Utd_Football"
    _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")
    _write_jpg(klh_config["card_dir"] / f"{stem}.jpg")

    calls = {}

    class _FakeSpec:
        id = "a4-a-mount"
        output_format = "jpg"
        output_quality = 85
        slots = {"picture": None, "card": None}

    def fake_load_spec(template_id, **kw):
        calls["template_id"] = template_id
        return _FakeSpec()

    def fake_composite(spec, *, picture_path, card_path, name, secondary_path=None):
        calls["name"] = name
        calls["picture"] = Path(picture_path)
        calls["card"] = Path(card_path) if card_path else None
        return Image.new("RGB", (10, 10), (0, 255, 0))

    def fake_save_mockup(img, out_path, spec):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "JPEG", quality=80)

    monkeypatch.setattr(pcomp, "load_spec", fake_load_spec)
    monkeypatch.setattr(pcomp, "composite", fake_composite)
    monkeypatch.setattr(pcomp, "save_mockup", fake_save_mockup)

    r = client.post(
        "/api/mockup",
        json={"product_key": "a4_mount_a", "pair_key": stem},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["is_raw_photo"] is False
    assert data["template_id"] == "a4-a-mount"
    assert data["mockup_url"].startswith("/api/mockup-image/a4_mount_a__")
    assert Path(data["mockup_path"]).exists()

    # Compositor was called with the right inputs
    assert calls["template_id"] == "a4-a-mount"
    assert calls["name"] == "Wayne Rooney"
    assert calls["picture"].name == f"{stem}.jpg"
    assert calls["card"] is not None


def _write_oriented_jpg(path: Path, size) -> Path:
    """Write a JPG with explicit dimensions so orientation is testable."""
    from PIL import Image
    img = Image.new("RGB", size, color=(200, 200, 200))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=80)
    return path


@pytest.mark.parametrize(
    "pixel_size, expected_template",
    [
        ((40, 30), "10x8-mount-land"),   # wider than tall → landscape
        ((30, 40), "10x8-mount-port"),   # taller than wide → portrait
    ],
)
def test_mockup_10x8_auto_detects_orientation(
    client, klh_config, monkeypatch, pixel_size, expected_template,
):
    """
    10x8 mount has orientation_lock: auto. When the frontend doesn't
    send req.orientation, /api/mockup must read the scan's dimensions
    and pick 10x8-mount-land vs 10x8-mount-port so compositor.load_spec
    finds a real template folder. Without this, pick_template_id falls
    back to "10x8-mount" (no such folder) and the endpoint 404s.
    """
    from PIL import Image

    stem = "Steve McQueen_Bullitt_Film"
    _write_oriented_jpg(klh_config["picture_dir"] / f"{stem}.jpg", pixel_size)
    # 10x8 has no card — deliberately leave card_dir empty.

    captured = {}

    class _FakeSpec:
        output_format = "jpg"
        output_quality = 85
        slots = {"picture": None}

    def fake_load_spec(template_id, **kw):
        captured["template_id"] = template_id
        spec = _FakeSpec()
        spec.id = template_id
        return spec

    def fake_composite(spec, *, picture_path, card_path, name, secondary_path=None):
        return Image.new("RGB", (10, 10), (0, 0, 255))

    def fake_save_mockup(img, out_path, spec):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "JPEG", quality=80)

    monkeypatch.setattr(pcomp, "load_spec", fake_load_spec)
    monkeypatch.setattr(pcomp, "composite", fake_composite)
    monkeypatch.setattr(pcomp, "save_mockup", fake_save_mockup)

    r = client.post(
        "/api/mockup",
        json={"product_key": "10x8_mount", "pair_key": stem},
    )
    assert r.status_code == 200, r.text
    assert r.json()["template_id"] == expected_template
    assert captured["template_id"] == expected_template


@pytest.mark.parametrize(
    "pixel_size, expected_template",
    [
        # 10x8 = 1.25 aspect, 12x8 = 1.5 aspect. Threshold is 1.375.
        ((50, 40),  "16x12-e-mount"),   # 1.25  landscape 10x8 → E
        ((40, 50),  "16x12-f-mount"),   # 1.25  portrait  10x8 → F
        ((60, 40),  "16x12-c-mount"),   # 1.5   landscape 12x8 → C
        ((40, 60),  "16x12-d-mount"),   # 1.5   portrait  12x8 → D
    ],
)
def test_mockup_16x12_cdef_auto_routes_by_photo_size_and_orientation(
    client, klh_config, monkeypatch, pixel_size, expected_template,
):
    """
    16x12 CDEF has main_size: auto + orientation_lock: auto. From the
    scan's dimensions alone, /api/mockup must pick one of four
    templates via the compound variant key "{photo_size}_{orientation}":

        12x8 landscape → 16x12-c-mount
        12x8 portrait  → 16x12-d-mount
        10x8 landscape → 16x12-e-mount
        10x8 portrait  → 16x12-f-mount
    """
    from PIL import Image

    stem = "Test Player_England_Football"
    _write_oriented_jpg(klh_config["picture_dir"] / f"{stem}.jpg", pixel_size)
    # No card — 16x12 CDEF is a one-photo layout, needs_secondary: null.

    captured = {}

    class _FakeSpec:
        output_format = "jpg"
        output_quality = 85
        slots = {"picture": None}

    def fake_load_spec(template_id, **kw):
        captured["template_id"] = template_id
        spec = _FakeSpec()
        spec.id = template_id
        return spec

    def fake_composite(spec, *, picture_path, card_path, name, secondary_path=None):
        return Image.new("RGB", (10, 10), (255, 0, 0))

    def fake_save_mockup(img, out_path, spec):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "JPEG", quality=80)

    monkeypatch.setattr(pcomp, "load_spec", fake_load_spec)
    monkeypatch.setattr(pcomp, "composite", fake_composite)
    monkeypatch.setattr(pcomp, "save_mockup", fake_save_mockup)

    r = client.post(
        "/api/mockup",
        json={"product_key": "16x12_mount_cdef", "pair_key": stem},
    )
    assert r.status_code == 200, r.text
    assert r.json()["template_id"] == expected_template
    assert captured["template_id"] == expected_template


def test_mockup_image_serves_back(client, klh_config):
    """A JPG sitting in mockups_dir is served by /api/mockup-image/<name>."""
    out = _write_jpg(klh_config["mockups_dir"] / "demo.jpg", color=(10, 20, 30))
    r = client.get("/api/mockup-image/demo.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content[:3] == b"\xff\xd8\xff"  # JPEG magic


def test_mockup_image_rejects_traversal(client, klh_config):
    r = client.get("/api/mockup-image/..%2Fconfig.yaml")
    assert r.status_code in (400, 404)


# --------------------------------------------------------------------------- #
# /api/list
# --------------------------------------------------------------------------- #

def _fake_trading_setup(monkeypatch):
    """
    Replace upload_site_hosted_picture + verify/submit/schedule_listing
    with in-memory fakes. Returns a dict the tests can inspect.
    """
    captured = {
        "uploads":   [],
        "verified":  [],
        "submitted": [],
        "scheduled": [],
    }

    def fake_upload(path, **kw):
        captured["uploads"].append(Path(path))
        return f"https://i.ebayimg.com/fake/{Path(path).name}"

    def fake_verify(listing, urls):
        captured["verified"].append((listing, list(urls)))
        return {"ack": "Success", "fees": [], "warnings": [], "item_id": None}

    def fake_submit(listing, urls, *, confirm=False):
        assert confirm is True, "submit must be called with confirm=True"
        captured["submitted"].append((listing, list(urls)))
        return {"ack": "Success", "fees": [], "warnings": [], "item_id": "999"}

    def fake_schedule(listing, urls, schedule_time, *, confirm=False):
        assert confirm is True
        captured["scheduled"].append((listing, list(urls), schedule_time))
        return {"ack": "Success", "fees": [], "warnings": [], "item_id": "888"}

    monkeypatch.setattr(plister, "upload_site_hosted_picture", fake_upload)
    monkeypatch.setattr(plister, "verify_listing", fake_verify)
    monkeypatch.setattr(plister, "submit_listing", fake_submit)
    monkeypatch.setattr(plister, "schedule_listing", fake_schedule)
    return captured


def test_list_verify_only_photo_product(client, klh_config, monkeypatch):
    """
    Happy path: verify_only=true (the default), photo_10x8, valid .99
    price. Picture gets uploaded, verify_listing called, and the
    response carries the listing summary.
    """
    captured = _fake_trading_setup(monkeypatch)

    stem = "Wayne Rooney_Man Utd_Football"
    _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")

    r = client.post(
        "/api/list",
        json={
            "product_key": "photo_10x8",
            "pair_key": stem,
            "price_gbp": 19.99,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["action"] == "verify"
    assert len(captured["uploads"]) == 1
    assert len(captured["verified"]) == 1
    assert captured["submitted"] == []
    # Summary surfaces key fields
    summary = data["summary"]
    assert "Wayne Rooney" in summary["title"]
    assert summary["price_gbp"] == 19.99
    assert summary["best_offer"] is not None
    assert summary["best_offer"]["list_price"] == 19.99


def test_list_verify_only_templated_product_needs_mockup(
    client, klh_config, monkeypatch
):
    """
    Templated product + no mockup on disk → 404 pointing the user at
    /api/mockup.
    """
    _fake_trading_setup(monkeypatch)
    stem = "Wayne Rooney_Man Utd_Football"
    _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")
    _write_jpg(klh_config["card_dir"] / f"{stem}.jpg")

    r = client.post(
        "/api/list",
        json={
            "product_key": "a4_mount_a",
            "pair_key": stem,
            "price_gbp": 39.99,
        },
    )
    assert r.status_code == 404
    assert "mockup" in r.json()["detail"].lower()


def test_list_verify_only_templated_product_uses_mockup_file(
    client, klh_config, monkeypatch
):
    """With a mockup file sitting in mockups_dir, /api/list uploads it."""
    captured = _fake_trading_setup(monkeypatch)

    stem = "Wayne Rooney_Man Utd_Football"
    _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")
    _write_jpg(klh_config["card_dir"] / f"{stem}.jpg")
    # Pre-create the mockup the endpoint will look for.
    mockup_name = f"a4_mount_a__{stem}.jpg"
    _write_jpg(klh_config["mockups_dir"] / mockup_name)

    r = client.post(
        "/api/list",
        json={
            "product_key": "a4_mount_a",
            "pair_key": stem,
            "price_gbp": 39.99,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["action"] == "verify"
    # Uploaded the mockup, not the raw scan
    assert len(captured["uploads"]) == 1
    assert captured["uploads"][0].name == mockup_name


def test_list_refuses_live_without_confirm(client, klh_config, monkeypatch):
    """verify_only=False but confirm missing → 400."""
    _fake_trading_setup(monkeypatch)
    stem = "Wayne Rooney_Man Utd_Football"
    _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")

    r = client.post(
        "/api/list",
        json={
            "product_key": "photo_10x8",
            "pair_key": stem,
            "price_gbp": 19.99,
            "verify_only": False,
            "confirm": False,
        },
    )
    assert r.status_code == 400
    assert "confirm" in r.json()["detail"].lower()


def test_list_submit_live_with_confirm(client, klh_config, monkeypatch):
    """verify_only=False + confirm=true → submit path taken."""
    captured = _fake_trading_setup(monkeypatch)
    stem = "Wayne Rooney_Man Utd_Football"
    _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")

    r = client.post(
        "/api/list",
        json={
            "product_key": "photo_10x8",
            "pair_key": stem,
            "price_gbp": 19.99,
            "verify_only": False,
            "confirm": True,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["action"] == "submit"
    assert data["result"]["item_id"] == "999"
    assert len(captured["submitted"]) == 1
    assert captured["verified"] == []


def test_list_schedule(client, klh_config, monkeypatch):
    """schedule_at set + confirm=true → schedule path."""
    captured = _fake_trading_setup(monkeypatch)
    stem = "Wayne Rooney_Man Utd_Football"
    _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")

    future = datetime.now(timezone.utc) + timedelta(hours=2)
    r = client.post(
        "/api/list",
        json={
            "product_key": "photo_10x8",
            "pair_key": stem,
            "price_gbp": 19.99,
            "verify_only": False,
            "confirm": True,
            "schedule_at": future.isoformat(),
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["action"] == "schedule"
    assert data["result"]["item_id"] == "888"
    assert len(captured["scheduled"]) == 1


def test_list_cached_upload_reuses_picture_url(client, klh_config, monkeypatch):
    """
    Two calls to /api/list for the same picture should only invoke
    upload_site_hosted_picture once — the second call hits the in-memory
    cache keyed on (path, mtime, size). This protects us from eBay's
    UploadSiteHostedPictures daily quota.
    """
    # Reset the cache for this test (it's a module-level dict and other
    # tests in this file may have populated it).
    from dashboard import workflow as wf
    wf._PICTURE_URL_CACHE.clear()

    captured = _fake_trading_setup(monkeypatch)
    stem = "Wayne Rooney_Man Utd_Football"
    _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")

    for _ in range(3):
        r = client.post(
            "/api/list",
            json={
                "product_key": "photo_10x8",
                "pair_key": stem,
                "price_gbp": 19.99,
            },
        )
        assert r.status_code == 200, r.text

    # Three verify calls, but only ONE upload round-trip.
    assert len(captured["verified"]) == 3
    assert len(captured["uploads"]) == 1


def test_list_cache_invalidates_on_file_change(client, klh_config, monkeypatch):
    """
    If the picture file changes (mtime bump), the cache key changes
    too and the next /api/list call re-uploads. This keeps the cache
    from serving a stale URL after Nicky re-renders a mockup.
    """
    import os, time
    from dashboard import workflow as wf
    wf._PICTURE_URL_CACHE.clear()

    captured = _fake_trading_setup(monkeypatch)
    stem = "Wayne Rooney_Man Utd_Football"
    path = klh_config["picture_dir"] / f"{stem}.jpg"
    _write_jpg(path)

    # First call → 1 upload
    r = client.post(
        "/api/list",
        json={"product_key": "photo_10x8", "pair_key": stem, "price_gbp": 19.99},
    )
    assert r.status_code == 200
    assert len(captured["uploads"]) == 1

    # Bump mtime by a millisecond so the cache key changes — ns
    # resolution means we can't just re-write, we have to touch.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    r = client.post(
        "/api/list",
        json={"product_key": "photo_10x8", "pair_key": stem, "price_gbp": 19.99},
    )
    assert r.status_code == 200
    # Second upload happened because the cache key is different now
    assert len(captured["uploads"]) == 2


# --------------------------------------------------------------------------- #
# /api/preview (pure, no eBay)
# --------------------------------------------------------------------------- #

def test_preview_returns_title_and_best_offer(client, klh_config):
    """
    /api/preview is pure — no uploads, no eBay round-trip. It should
    return the rendered title + best-offer dict so the frontend can
    populate its editable title field before the first Verify call.
    """
    r = client.post(
        "/api/preview",
        json={
            "product_key": "a4_mount_a",
            "pair_key":    "Wayne Rooney_Man Utd_Football",
            "price_gbp":   39.99,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    summary = data["summary"]
    assert "Wayne Rooney" in summary["title"]
    assert len(summary["title"]) <= 80
    assert summary["price_gbp"] == 39.99
    assert summary["best_offer"] is not None
    assert summary["item_specifics_count"] > 0


def test_preview_rejects_unknown_product(client, klh_config):
    r = client.post(
        "/api/preview",
        json={
            "product_key": "does_not_exist",
            "pair_key":    "Wayne Rooney_Man Utd_Football",
            "price_gbp":   39.99,
        },
    )
    assert r.status_code == 404


def test_list_title_override_wins(client, klh_config, monkeypatch):
    """
    If the caller sends a title_override, the built listing should use
    it verbatim (the user's manual edit wins over the rendered title).
    """
    captured = _fake_trading_setup(monkeypatch)
    stem = "Wayne Rooney_Man Utd_Football"
    _write_jpg(klh_config["picture_dir"] / f"{stem}.jpg")

    custom = "Wayne Rooney Hand-Signed Photo COA Manchester Legend"
    r = client.post(
        "/api/list",
        json={
            "product_key":    "photo_10x8",
            "pair_key":       stem,
            "price_gbp":      19.99,
            "title_override": custom,
        },
    )
    assert r.status_code == 200, r.text
    # The listing that went into verify_listing carries our custom title
    assert len(captured["verified"]) == 1
    built_listing, _urls = captured["verified"][0]
    assert built_listing["title"] == custom
    # And the endpoint echoes it back in the summary
    assert r.json()["summary"]["title"] == custom


def test_list_missing_picture_404(client, klh_config, monkeypatch):
    _fake_trading_setup(monkeypatch)
    r = client.post(
        "/api/list",
        json={
            "product_key": "photo_10x8",
            "pair_key": "Ghost_Nowhere_Cricket",
            "price_gbp": 19.99,
        },
    )
    assert r.status_code == 404
