# klh-listing-tool

Listing automation for Kim's eBay store (KLHAutographs).

Pipeline: scanned images → match → normalize → mockup → eBay listing via Trading API.

## Layout

```
ebay_api/          eBay API clients (Trading, later Inventory)
  token_manager.py OAuth token lifecycle — reads ~/.klh/
pipeline/          Image processing + listing pipeline
  config.py        Reads ~/.klh/config.yaml
  matcher.py       Phase 1 — Picture/Card pair + typo detection
  normalize.py     Phase 2 — format/size normalization
  compositor.py    Phase 3 — Pillow-based template compositor
  text_fit.py      Fit-to-box text measurement
  lister.py        Phase 6 — spec → Trading API payload
templates/         One folder per product template (yaml + base/overlay PNGs)
presets/           Product type definitions + listing boilerplate
cli/klh.py         Main command-line entry point
scripts/           Helper tools (PSD inspection, etc.)
tests/             Unit tests + visual regression fixtures
```

## Per-machine config

Machine-specific paths and credentials live at `~/.klh/`:

```
~/.klh/
  config.yaml      paths to Picture/Card/Products/Working/golden dirs
  .env             eBay app credentials (APP_ID, CERT_ID, etc.)
  tokens.json      OAuth access + refresh tokens
```

None of that is committed. Copy `config.yaml.example` → `~/.klh/config.yaml`
and edit paths for the current machine.

## Setup

```bash
cd /Volumes/Samsung_990_4TB/KLH/klh-listing-tool
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## CLI

```bash
klh match            # Phase 1 — pair pictures with cards, report mismatches
klh normalize        # Phase 2 — convert to jpg, standardize size
klh mockup <type>    # Phase 3 — generate mockups for a product type
klh list <type>      # Phase 6 — push listings to eBay Trading API
klh token            # Show token status / force refresh
```

## Phase status

- [x] Phase 0 — scaffolding, config, credentials migration
- [ ] Phase 1 — matcher CLI
- [ ] Phase 2 — normalizer
- [ ] Phase 3 — A4-A Mount compositor (reference template)
- [ ] Phase 4 — port remaining templates
- [ ] Phase 5 — product type presets
- [ ] Phase 6 — Trading API lister
- [ ] Phase 7 — local web UI
- [ ] Phase 8 — Inventory API migration
