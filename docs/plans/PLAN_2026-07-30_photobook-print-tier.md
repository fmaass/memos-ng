# PLAN 2026-07-30 — Photo-book print tier + flexible export API

Status: **shipped** (app/services/photobook.py; #74, #127). Derived from a live-measured design consultation
(2026-07-28/30). GitHub issue: see "Tracking" below.

## Problem (measured, not assumed)

Fabian assembles photo books in the Pixum desktop software from memos photos. The
software consumes >20 GB RAM and climbs during import. Cause is the images, not the
software: memos serves full-resolution masters, and Pixum's own published system
requirement is 2 GB RAM with a 130-page book using ~1.5 GB.

Live prod facts (measured 2026-07-30, container `memos-ng-app`, DB `memosng`):

- Curated image set: **238 attachments**, **18.212 GB** of originals.
- Mime split: **214 PNG, 24 JPEG, 0 HEIC** — the bloat is lossless PNG at up to
  9504x6336 (60 MP, 82-145 MB per file).
- Median source dimensions ~6336x6336; max 9504x6336.
- A 9504x6336 image costs **~180 MB decoded** regardless of source codec.

Two distinct failures hide behind one symptom:
1. **Memory** — driven purely by pixel count. Fixed by resizing.
2. **Upload rejection** — Pixum caps uploads at **50 MB/file**; the PNG masters exceed
   it. Fixed by encoding JPEG.
Both fixes are required; neither alone is sufficient.

## CORRECTED PREMISES — read before trusting older notes

Three claims made earlier in the source session were wrong and are corrected here.
A fresh session MUST NOT re-derive from the stale versions:

1. **`attachments.print_path` is NULL for every row in production** (verified twice,
   count = 0). Print masters are NOT in that column. 41 print renditions live in
   **`attachment_renditions`** (`variant_key LIKE 'print%'`, `app/models.py:165-188`).
   Any earlier "avg print master ~68 MB" figure was the average size of the
   *originals* of print-marked attachments — a conflation, not a print-master size.
2. **Curated count is filter-dependent**: 220 by memo-tag join, **238** by the filter
   the shipped code actually uses (tag `curated` + image-mime, mirroring
   `app/scripts/backfill_frame_album.py:84-94`). Originals total 18.212 GB, not 15.6.
3. **No HEIC in the curated set.** The HEIC-incompatibility risk raised earlier does
   not apply to this data (it remains true that the print encoder must always emit
   JPEG, which makes the question moot).

**REQUIRED FIRST PROBE for the implementing session:** decide and document which
selector is authoritative for "the print set" — `attachment_renditions` print rows,
the `#curated` tag, or an explicit per-book selection — and confirm the count against
the live DB before any code depends on it.

## Design

### Two axes, deliberately decoupled

- **Selection is dynamic** (a query — cheap, arbitrarily flexible): time range, tag,
  memo ids, print-marked, owner.
- **Rendition is a fixed ladder** of pre-generated variants. Arbitrary sizes on the
  request path are forbidden by the project invariant "serve pre-generated variants,
  never re-encode on request" (and would be brutally slow on 40-60 MP sources).

### Size: ONE rung at 4096 px long edge

Geometry that fixes the number: the book is 21x21 cm layflat; at 300 dpi (Pixum's
stated minimum) 21 cm = 2480 px. Because the book is square and the photos are 3:2,
a photo at full page height spans exactly 1.5 pages = 31.5 cm = **3720 px** — the
largest thing ever printed. Plus ~35 px/edge bleed, rounded up: **4096** (also exactly
2x the existing `mobile` tier; prints to 34.7 cm at 300 dpi, so it survives an upgrade
to 30x30 books for single-page images).

**Per-image rung snapping was considered and rejected on the measurements**: only
14 of 238 images are square-ish (aspect 0.9-1.1), so a smart rule saves 6%
(1.03 GB vs 1.09 GB). Not worth the complexity. Keep the ladder concept in the API
shape only if a genuinely different book format appears later.

### Measured output sizes (10 real prod images, q92/4:4:4/300dpi)

| Rung | Median | Mean | Range | Whole 238-image set |
|---|---|---|---|---|
| 2560 | 1.13 MB | 1.62 MB | 0.53-5.18 MB | 0.37 GB (2% of today) |
| 3072 | 1.80 MB | 2.46 MB | 0.73-7.64 MB | ~0.7 GB |
| **4096** | **3.72 MB** | 4.77 MB | 1.27-14.04 MB | **1.09 GB (6% of today)** |

Bytes/megapixel at 4096 = 395,318 (the extrapolation constant). Content drives size
as much as pixels: three ~60 MP 3:2 images produced 5.00 / 4.32 / 8.86 MB at identical
dimensions. The 14 MB outlier is the one 5989x5989 square (fills the whole 4096 box).

Quality curve at 4096 (mean, % of q92/4:4:4): q85 = 2.97 MB (62%), **q92 = 4.77 MB**,
q95 = 6.55 MB (137%), q92 at 4:2:0 = 3.43 MB (72%). Recommendation: stay q92/4:4:4 —
the saving is irrelevant at a ~1 GB total and chroma artefacts show on gloss paper.

### Encode profile (all of these matter for print; none are set today)

Current tier saves are `img.save(tmp, "JPEG", quality=80)` with no other kwargs
(`app/services/media.py:27-31`) — fine for screen, wrong for 382 g/m² gloss:

- `quality=92`, `subsampling=0` (4:4:4), `optimize=True`
- **`dpi=(300,300)`** — nothing writes DPI today, so layout software assumes 72 dpi
  and mis-scales on placement.
- **Colour-managed conversion to sRGB with the profile embedded.** `.convert("RGB")`
  (`media.py:49`) is a mode change, not a colour transform; AdobeRGB/P3 sources print
  dull. Pixum requires sRGB and rejects CMYK.
- **Always JPEG**, whatever the source.
- Orientation already correct via `ImageOps.exif_transpose` (`media.py:49`) — keep.
- Source preference: the print rendition if one exists (it is the user's edited crop),
  else the original.

## Code touchpoints (verified 2026-07-30)

- Tiers are hard-coded blocks, no config table: `THUMB_MAX=800`, `MOBILE_MAX=2048`
  (`app/services/media.py:19-20`), generated in `process_image` (`:40-65`) via
  `img.copy().thumbnail((MAX,MAX))` + `_atomic_save` into `_assets("<subdir>")`.
  Adding a rung = one constant + one block + extend the `TIERS` tuple
  (`app/routers/assets.py:26`) and `_derived_tier_path()` (`assets.py:29-40`).
- `GET /api/attachments/{uid}/{tier}` = `serve_asset` (`assets.py:261`); `print`
  resolves via `_tier_path` (`assets.py:43-51`) and streams stored bytes **verbatim,
  never resized** (`assets.py:55-69`). Do NOT change `print`'s meaning — satellites may
  use it. Expose the new rendition under its own key (e.g. `printready`).
- `POST /api/attachments/{uid}/print` = `upload_print` (`assets.py:359-403`), owner-only,
  images only, sets `print_path`/`print_mime`/`print_size` — unused in prod (see
  Corrected Premises #1).
- Jobs: `media_jobs` (`app/models.py:207-225`), lanes fast|slow. **No on-demand enqueue
  path exists for existing attachments** — one must be written (mirror
  `migrate_from_memos.py:197`). Note `backfill_media_metadata.py:18-19` deliberately
  avoids enqueuing jobs because that would regenerate tiers.
- Bulk export: `GET /api/export` (`app/routers/export.py:23-88`) zips **memo text only**;
  `export.py:34-36` carries an explicit TODO for a `tier` param — that is the hook.
  No other zip/archive code exists repo-wide.

## Proposed API shape

```
GET /api/exports/photobook
    ?from=2026-01-01&to=2026-06-30      # dynamic selection
    &tag=curated
    &source=best                        # print rendition if present, else original
    &profile=print-4096                 # or page_cm=21x21&max_pages=1.5&dpi=300
    &format=zip                         # or "manifest"
```

- `format=manifest` → JSON per photo: URL, actual px, rung chosen, and the
  **effective dpi at the requested print size** (this is the highest-value field — it
  surfaces Pixum's warning-triangle information *before* import).
- `format=zip` → streamed zip, date-ordered numbered filenames so Pixum imports a
  folder in sequence.
- Generation for ~238 photos is not instant: enqueue jobs and report progress over the
  existing SSE channel rather than blocking the request.

## Cost / constraints

- Storage: ~1.1 GB for the full curated set at 4096 (vs 18.2 GB of originals).
- CPU/thermal: a one-off re-encode of 238 x 40-60 MP images on the shared host. Use the
  slow lane, single-threaded, throttled. (Reference: a 10-image sample took 45.6 s.)
- Decoded memory for Pixum drops ~5.4x (180 MB -> 34 MB per image).

## Open decisions (need the owner)

1. Authoritative "print set" selector (see REQUIRED FIRST PROBE).
2. Whether the export selection should be ad-hoc query only, or a persisted named
   "book" (an ordered set of photos) — the latter is more useful long-term since a
   book is really a named, ordered selection.
3. Whether to add the smaller rungs (2560/3072) at all, given the single-rung finding.

## Tracking

GitHub issue filed in `fmaass/memos-ng` (see repo issues, title
"Photo-book print tier + flexible export API"). Session context: BookStack session
review p2585 (2026-07-25 media-ui-batch, appended 2026-07-30).
