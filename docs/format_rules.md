# KLH size → format rules

Locked in by Peter 2026-04-18 after Clarkson over-correction incident.

These rules govern what the **Format** token in a title + the **Type** item
specific must be, based on the physical size string in the listing. This
lives outside code because the ambiguity at 10x8 and 16x12 needs human
judgement — the cache cannot derive it.

## Rules

| Size  | Title format                   | Notes                           |
| ----- | ------------------------------ | ------------------------------- |
| 6x4   | `Photo`                        | Loose photo, never a display    |
| 10x8  | `Photo` **or** `Photo Display` | AMBIGUOUS — check physical item |
| 12x8  | `Photo`                        | Loose photo, never a display    |
| A4    | `Photo Display`                | No loose A4 photos at KLH       |
| A3    | `Photo`                        | Loose photo, never a display    |
| 16x12 | `Photo` **or** `Photo Display` | AMBIGUOUS at the dataset level. **Per-signer overrides** below may remove the ambiguity. |

## Per-signer 16x12 overrides

When a signer has no loose 16x12 photos at all (only displays), record it
here so recategorisation scripts can auto-ensure Display without CSV review.

| Signer            | All 16x12 are Display? |
| ----------------- | ---------------------- |
| Jeremy Clarkson   | yes (no loose 16x12)   |

## Prefixed variants (always keep Display)

* `Framed` → always `Framed Photo Display`
* `Mount` / `Mounted` → always `Mounted Photo Display`

## Why this file exists

On 2026-04-18 the Clarkson full-treatment pass (scripts/apply_clarkson_is.py)
ran twice:

1. First pass: derived `ptype=Photo` for every non-Framed, non-Mount item →
   stripped "Display" from all 10x8 / 16x12 / A4 titles that originally
   had it.
2. Second pass: tried to fix by forcing `Photo Display` on *all* 10x8 /
   A4 / 12x8 / A3 / 16x12 — over-correcting the non-display sizes.

Ground truth was lost from the cache (we overwrote `title`). Recovery
needed a CSV review for the ambiguous sizes (`scripts/fix_clarkson_formats.py
--export-csv ...`).

## Going forward

Any signer-rollout script that rewrites titles **must**:

1. Hard-code this table (not re-derive it from title text).
2. For ambiguous sizes (10x8, 16x12), **preserve the original Photo/Photo
   Display token** from the pre-rewrite title unless explicitly overridden
   by a reviewed CSV.
3. Archive the original title to an `original_title` column on first touch,
   so future passes can recover without Time Machine.
