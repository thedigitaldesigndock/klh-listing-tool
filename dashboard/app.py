"""
FastAPI application factory for the KLH dashboard.

`create_app()` builds and returns a FastAPI instance with:

    GET  /                        → static index.html shell
    GET  /static/*                → JS / CSS / icons
    GET  /api/health              → liveness probe ({"status":"ok"})
    GET  /api/products            → full 22-product catalog
    GET  /api/config              → ONE/ and TWO/ folder paths + flags
    GET  /api/template-preview/*  → static mockup preview thumbnails
    GET  /api/match               → pair ONE/ + TWO/, return report
    POST /api/mockup              → render one mockup (synchronous)
    GET  /api/mockup-image/*      → serve rendered mockup back
    POST /api/list                → verify / schedule / submit listing

The PresetsBundle is loaded ONCE at startup and stashed on
`app.state.bundle`. The config (~/.klh/config.yaml) is re-read on each
workflow call so Nicky can edit it without a restart, but it's cheap
(a few dozen lines of YAML) so that's fine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pipeline import presets as pp
from pipeline import config as pcfg

from dashboard.catalog import build_catalog
from dashboard.workflow import register_workflow_routes


STATIC_DIR    = Path(__file__).resolve().parent / "static"
REPO_ROOT     = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"

# Only these filenames can be fetched via /api/template-preview/<id>.
# We deliberately restrict to the preview JPEG so the route can't be
# abused to walk out of templates/ or leak spec.yaml / source.json.
_ALLOWED_PREVIEW_NAME = "preview.jpg"


def create_app() -> FastAPI:
    """Build the FastAPI app. Called from dashboard.server at boot."""
    app = FastAPI(
        title="KLH Listing Dashboard",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
    )

    # Preload the presets bundle ONCE at app-construction time. Parsing
    # three YAML files is ~1ms, so there's no reason to defer it behind
    # a lifespan handler — doing it eagerly also means TestClient can
    # hit the endpoints without entering a lifespan context.
    app.state.bundle = pp.load()

    # --- Static assets (JS/CSS/icons live under /static). -------------- #
    if STATIC_DIR.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

    # --- Routes -------------------------------------------------------- #

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """Serve the SPA shell."""
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/products")
    def api_products() -> JSONResponse:
        """Return the full 22-product catalog + tile layout."""
        # Rebuild each call so edits to products.yaml in dev show up on
        # a browser refresh without a server bounce. It's a few-ms pure-
        # python loop — no reason to cache.
        bundle = app.state.bundle
        return JSONResponse(build_catalog(bundle))

    @app.get("/api/template-preview/{template_id}")
    def api_template_preview(template_id: str) -> FileResponse:
        """
        Serve <repo_root>/templates/<template_id>/preview.jpg.

        Locked down to the single allowed filename so this route can't
        be abused to read spec.yaml, source.json, or walk out of the
        templates tree. Template IDs come from products.yaml and are
        slugs like "16x12-c-mount" — we reject anything with a slash
        or a leading dot as a belt-and-braces check.
        """
        if "/" in template_id or "\\" in template_id or template_id.startswith("."):
            raise HTTPException(status_code=400, detail="invalid template id")
        preview_path = TEMPLATES_DIR / template_id / _ALLOWED_PREVIEW_NAME
        # Resolve and confirm it's still inside TEMPLATES_DIR.
        try:
            resolved = preview_path.resolve(strict=True)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="preview not found")
        if TEMPLATES_DIR.resolve() not in resolved.parents:
            raise HTTPException(status_code=400, detail="invalid template id")
        return FileResponse(resolved, media_type="image/jpeg")

    @app.get("/api/config")
    def api_config() -> JSONResponse:
        """
        Return ONE/ and TWO/ paths with existence flags so the dashboard
        can show a red banner if the folders are missing.

        Shape:
            {
              "one": {"path": "/Users/nicky/Desktop/ONE", "exists": true},
              "two": {"path": "/Users/nicky/Desktop/TWO", "exists": true},
              "ok":  true
            }
        """
        try:
            cfg = pcfg.load()
        except pcfg.ConfigError as e:
            return JSONResponse(
                {"ok": False, "error": str(e)},
                status_code=500,
            )

        one_path = cfg.paths.picture_dir   # "ONE" = picture_dir in config
        two_path = cfg.paths.card_dir      # "TWO" = card_dir in config

        def _info(p: Path) -> dict[str, Any]:
            return {
                "path":   str(p) if p else None,
                "exists": bool(p and p.exists()),
            }

        one = _info(one_path)
        two = _info(two_path)
        return JSONResponse({
            "ok":  one["exists"] and two["exists"],
            "one": one,
            "two": two,
        })

    # --- Workflow routes (/api/match, /api/mockup, /api/list) --------- #
    register_workflow_routes(app)

    return app


# Module-level app instance for `uvicorn dashboard.app:app` invocation.
# dashboard.server uses the factory directly, so this is just a
# convenience for devs running uvicorn by hand.
app = create_app()
