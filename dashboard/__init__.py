"""
KLH Listing Dashboard — local FastAPI web UI wrapping the listing pipeline.

This package exposes:

    dashboard.app         — FastAPI application factory (`create_app`)
    dashboard.catalog     — builds the product-grid view model for /api/products
    dashboard.server      — uvicorn runner, invoked by `klh dashboard`

The dashboard reuses pipeline.* modules directly (matcher, compositor,
presets, lister) — no business logic lives here. This package is purely
a presentation layer:

    - GET  /                   → static index.html shell
    - GET  /api/products       → full 22-product catalog + dashboard layout
    - GET  /api/config         → ONE/ and TWO/ folder paths, verify they exist
    - POST /api/match          → run matcher on ONE/ vs TWO/, return report
    - POST /api/mockup         → composite one pair, return preview JPEG
    - POST /api/list           → list one pair with a user-supplied price

Design notes
------------
* Server-side state is deliberately minimal. The frontend drives the
  flow; the backend is stateless except for the preloaded PresetsBundle
  and an eBay API client.
* Mockups and pictures live on disk under ~/.klh/dashboard/runs/<ts>/;
  the browser fetches them via static file routes. No base64 in JSON.
* No auth. Dashboard binds to 127.0.0.1 only. If Kim ever needs to
  access Nicky's dashboard remotely that's a Phase 7 concern.
"""
