# PLAN — Screen/print variant framework (filename-convention renditions)

Status: **DRAFT — review before implementing.** No code is written yet.
- Rev 1 → Rev 2: Codex adversarial review (3 HIGH + 4 MEDIUM resolved; APPROVE).
- Rev 2 → Rev 3: independent 3-lens cross-validation (code-fidelity / completeness / security, each finding
  adversarially verified). Core design **confirmed sound**; 9 real gaps folded in (below).

## Context for Independent Review

**System.** memos-ng (FastAPI + SQLAlchemy 2 async + Postgres; Vue 3 SPA), live in production, shares
Postgres17 + the thermally-bound Saturn NAS with a 7-day decommission soak of old memos. A photo today is
one `Attachment` row: `storage_path` is the **screen/web master** (the "original" on download/export), the
worker derives `thumb/mobile/web/poster` tiers from it, and there is a **single** optional print rendition
(`print_path/print_mime/print_size`, uploaded via `POST /api/attachments/{uid}/print`, served download-only
on the auth path).

**Goal.** Let the owner **dump multiple physical files for one logical photo** using a Lightroom-style
filename convention: the **screen** file is the normal web picture; one or more **print** files are
*hidden from the web but retrievable via the API*. Generalize the single `print_path` slot into **many
named renditions**.

**Naming convention (owner-confirmed, strict):**
```
_AR55821.png                       → screen (the web master; treated exactly like a normal picture today)
_AR55821 - screen.png              → screen (explicit; identical to no-suffix)
_AR55821 - print (glossy).png      → print rendition, label "glossy"  → variant_key = print:glossy
_AR55821 - print (matte).png       → print rendition, label "matte"   → variant_key = print:matte
_AR55821 - print.png               → print rendition, no label        → variant_key = print
```

**Owner decisions (confirmed) that shape the build:**
1. **Strict grammar.** Only ` - print (<label>)` / ` - print` / ` - screen` are special; everything else is
   a normal screen picture.
2. **`print` is the only hidden role.** The hide test is "is this a `print`?". Anything else is a normal
   curated picture; a typo'd suffix stays **visible** — nothing is ever silently hidden.
3. **No migration.** Owner re-uploads; existing `print_path` + the legacy endpoint left **fully intact**
   (additive only; no backfill, no column drop).
4. **A print with no matching screen in its dump → PROMOTED to a normal visible picture.** No orphan
   table/reaper/recovery. ⇒ grouping is **IN-BATCH ONLY**.
5. **Print download = household-visible** (owner-confirmed): the new rendition list/download endpoints use
   `_viewable`, exactly like the existing `print` tier — anyone who can see a shared photo can download its
   print masters. (Recorded so it's a deliberate choice, not an accident.)
6. **Rendition file types = any mime, download-only** (default unless revisited): a rendition is an archive
   master (PNG/TIFF/RAW/etc.), stored hidden, zero-MediaJob, served download-only. Not restricted to images
   (unlike the legacy `/print` endpoint).

## Architecture

### Hiding is structural (the load-bearing property — cross-validation-confirmed)
Every web/compat/SSE/export surface only ever traverses `Attachment`/`Memo` rows: `_att_ref`/`_out`
(`memos.py:107,140`), `_list_item`/`list_attachments` (`assets.py:128,146`), `attachment_meta`
(`assets.py:172`), compat `_compat_memo` (`compat.py:71-90`) + `/file/*` (`compat.py:168`, derived uid
paths only), SSE `_user_snapshot` (`assets.py:358`, Attachment by owner+status), and `export.py` (memo
text). A rendition is **not** an `Attachment` row, so it cannot enter any of these — no filter to forget.

### Data model — new table `attachment_renditions`
One row per hidden physical file (a `print`). The screen file stays the `Attachment` itself.

| column | type | notes |
|---|---|---|
| `id` | PK | |
| `attachment_id` | FK→`attachments.id` `ON DELETE CASCADE`, indexed | the screen photo owns its prints |
| `variant_key` | `String(96)` | `print` or `print:<slug>` |
| `label` | `String(48)\|None` | parsed sub-name; length-bounded + slugified |
| `filename` | `String(512)` | original dumped name (round-trip fidelity) |
| `mime` | `String(255)` | sniffed via the shared `_sniff_mime`/`detect_heif_mime` (any mime allowed) |
| `size` | `BigInteger` | |
| `sha256` | `String(64)\|None` | informational / export-manifest only — **NOT** used for file sharing |
| `storage_path` | `String(1024)` | a **unique** uuid-named file under `DATA_ROOT/print/` |
| `created_at` | server_default now() | |

- **UNIQUE `(attachment_id, variant_key)`**. A re-dump of the **same key** (same photo) is an idempotent
  no-op. **But an in-batch collision with DIFFERENT bytes is PROMOTED, not dropped** (gap #3): if two files
  in one batch resolve to the same `(screen, variant_key)` (e.g. `X - print (glossy).png` +
  `X - print (glossy).heic`), the first becomes the rendition and the second is **promoted to a visible
  Attachment** — never silently unlinked (consistent with decision 4: a print is never silently lost).
- **Unique uuid on-disk filenames** (`{uuid4hex}{ext}`) — never a deterministic shared path → no
  overwrite-committed-bytes, no extension leak, no shared-file deletion. No two rows reference one file.
- `Attachment.renditions` relationship (`lazy="selectin"`, `cascade="all, delete-orphan"`) — auto-chains
  under the existing `Memo.attachments` selectin (no N+1 for `hasPrint`). Renditions carry **no derived
  tiers and get NO MediaJob**.

### Parse grammar — `parse_variant(filename)` (pure, unit-tested per category)
`(base, is_print, label)`. Strip ext; match the stem: `… - print (label)` → print+label; `… - print` →
print; `… - screen` → screen; else → screen, base=stem. Then normalize `label`: trim, lowercase,
non-`[a-z0-9-]`→`-`, truncate 48; `variant_key = "print"` or `f"print:{slug}"` (path/URL-safe, can't carry
`/` or collide a separator). Regex anchored at stem-end (so `Trip - print shop.png` → screen). Test
categories: no-suffix, explicit-screen, print-bare, print-labeled, near-miss, label-with-spaces→slug,
over-length→truncated, weird-charset→slug, malformed-parens→screen.

### Ingest / grouping — two passes in `finalize_pending` (`uploads.py:119`), in-batch only
```
finalize_pending(db, upload_uids, owner_id, moved_out):
  parsed = {up: parse_variant(up.filename) for each pending+complete upload in the batch}
  base_to_attachment = {}
  # PASS 1 — screens become Attachments via finalize() (Attachment + MediaJob, unchanged)
  for up where not parsed[up].is_print:
      att = await finalize(db, up, owner_id, moved_out)     # finalize now owns moved_out (fix below)
      base_to_attachment.setdefault(parsed[up].base, att)   # FIRST-screen-wins
      out.append(att.uid)
  # PASS 2 — prints attach to their batch screen, else promote
  seen_keys = set()
  for up where parsed[up].is_print:
      screen = base_to_attachment.get(parsed[up].base)
      key = (screen.id, parsed[up].variant_key) if screen else None
      if screen is not None and key not in seen_keys:
          await finalize_rendition(db, up, screen, parsed[up], moved_out); seen_keys.add(key)
      else:                                                 # no screen OR in-batch key collision
          att = await finalize(db, up, owner_id, moved_out); out.append(att.uid)   # PROMOTE → visible
  return out   # renditions are NOT in `out`
```
Edge cases now explicit (gaps #4/#7/#8):
- **Two screens, same base** (`X.png` + `X - screen.png`): both become visible photos; prints bind to the
  first (setdefault). Tested + documented.
- **In-batch `(screen, variant_key)` collision**: the loser is promoted (above), not dropped.
- **Screen deduped to a pre-existing attachment**: `finalize` may return an existing (possibly older-memo)
  attachment; the print attaches to it, and `link_attachments` re-points it to the new memo — consistent
  with how memos-ng already moves deduped attachments between memos. Tested + documented.

**Pre-existing bug fixed here (Codex + cross-validation HIGH, uploads.py:141).** `finalize_pending` appends
`att.storage_path` to `moved_out` *even when `finalize` dedup-returned an existing committed attachment*
(uploads.py:164) → a rolled-back compose unlinks a **prior** memo's file. Fix: **move the
`moved_out.append` into `finalize`**, on the new-file branch only (right after `os.replace`, unreachable
from the dedup early-return). **`finalize`'s signature becomes
`finalize(db, upload, owner_id, moved_out: list[str] | None = None)` — `moved_out` OPTIONAL** so the other
callers keep compiling. **Caller audit (gap #2, definition-of-done):** update `finalize_pending` (passes
it) and confirm `routers/uploads.py:113` (one-shot `finalize_upload`) + `tests/test_uploads.py:131,275`
compile unchanged (they pass no `moved_out`). The legacy `attachment_uids` path routes through
`finalize_pending`, so it's covered.

### `finalize_rendition(db, upload, attachment, parsed, moved_out)` — new
1. If `(attachment.id, parsed.variant_key)` already exists → unlink part, return (idempotent — but see the
   in-batch-collision promote handled by the caller for DIFFERENT bytes).
2. `mime = _sniff_mime(part, upload.mime)` (any mime allowed; fixes HEIC). Compute `sha256` (informational).
3. `os.replace` part → `DATA_ROOT/print/{uuid4hex}{ext}` (unique). Append that path to `moved_out` (if not
   None).
4. Insert the rendition row.
5. **Invariant: ZERO MediaJob** (enqueue lives only in `finalize`, uploads.py:199) — asserted by a test,
   including for a HEIC/video/pdf rendition.

### Serve + read API (additive, auth path; household-visible per decision 5)
- **Route ordering (verified):** register `…/renditions` + `…/rendition/{variant_key}` **before** the
  generic `GET /{uid}/{tier}` (`assets.py:198`) so the literal segments match first (cross-validation
  confirmed Starlette matches in registration order; `print:glossy` routes fine as a `str` path param).
- `GET /api/attachments/{uid}/renditions` → `[{variant_key, label, mime, size, downloadUrl}]`, via
  `_viewable`.
- `GET /api/attachments/{uid}/rendition/{variant_key}` → download-only, short-session **auth path** (never
  the signed fast path), via `_viewable`.
- `AttachmentMeta` gains `renditions: [...]` **and** a `hasLegacyPrint: bool` (see Frontend); `hasPrint`
  becomes `print_path is not None OR len(renditions) > 0` at `memos.py:121`, `assets.py:138,191`
  (selectin-loaded).

### Frontend reconciliation (gap #1 — a regression the plan must prevent)
`MediaLightbox.vue:154` renders a "Download print version" button on `hasPrint && printUrl` → `…/print`.
Once `hasPrint` is an OR, a **renditions-only** photo would show that button pointing at a `print_path`
that is `None` → **404**. Fix in P3: the legacy single-print button is gated on the new **`hasLegacyPrint`**
(`print_path is not None`), and renditions are surfaced via the new **Prints list** (one download link per
`variant_key`). Unit-test the pure mapping; `MemoCard` print badge likewise keyed on `hasPrint` for "has
some print" but the download affordance distinguishes legacy vs renditions.

### Delete + cleanup
`_all_asset_paths` (`assets.py:242`) extended to enumerate every rendition `storage_path`; DB
`ON DELETE CASCADE` removes rows, `_unlink_all` removes the unique files. (ORM `delete-orphan` + DB CASCADE
mirror the existing `Memo.reactions` precedent.)

### Migration (additive only)
Single Alembic revision, `down_revision = 'b8951b65dc52'` (verified head): `CREATE TABLE` + indexes + the
unique constraint. No backfill, no `print_path` drop.

## Implementation sketch (phases — each closes with /session-review + /implementation-report + /converge)

### P1 — parse + model + migration
`app/services/variants.py` (`parse_variant`, `slug_label`, `variant_key_for`); `AttachmentRendition` +
relationship; additive Alembic create-table. Tests: every parse/slug category; alembic no-op.

### P2 — ingest + finalize_rendition + the moved_out fix + delete
`uploads.py`: `finalize(... moved_out=None)` (append moved into it, new-file branch); `finalize_rendition`;
two-pass grouping incl. promote-on-collision; **caller audit** (uploads.py:113 + the two tests).
`assets.py`: extend `_all_asset_paths`. Tests: hidden-rendition + feed/library/compat/SSE regression;
**moved_out rollback fix** (dedup re-upload + rollback does NOT unlink the original); zero-MediaJob (incl.
HEIC/video); promote (lone print); in-batch variant_key collision → promote; two-screens-same-base;
deduped-screen → rendition lands on the dedup target; write-once idempotent; delete → rows+unique files
gone, siblings untouched.

### P3 — read API + serializers + frontend reconciliation
`assets.py`: the two new routes (ordered first); `renditions` + `hasLegacyPrint` in `AttachmentMeta`;
`hasPrint` rewire (selectin). Frontend: gate the legacy print button on `hasLegacyPrint`; render the Prints
list from `renditions`; vitest the mapping. Tests: list/download authz (viewable yes; stranger 404/401);
`…/renditions` not shadowed; renditions-only photo shows NO broken legacy button. Regen openapi/schema.

### P4 — (optional) export round-trip — also builds the noted-unbuilt T2.4
`export.py`: add `tier`/`include_files`; bundle the screen original (named `Attachment.filename`, not the
on-disk `{uuid}_name`) + each rendition by convention; manifest carries `{variantKey,label,mime,sha256}`.
**Owner-scope files (gap #6):** the memo *list* is visibility-scoped (includes others' non-PRIVATE memos),
but **only the requester's OWN memos' files/renditions are bundled** — others' memos export text only.
Tests: export→re-ingest regroups identically; user B's export of a set incl. user A's memo bundles no A
files.

## Acceptance Criteria
- `X.png` + `X - print (glossy).png` in one compose → **one visible photo**; the print absent from feed,
  library, `/api/v1` + `/file/*` compat, and SSE; retrievable at `…/rendition/print:glossy` (auth,
  household-visible) + listed at `…/renditions` (not shadowed by `{tier}`); `hasPrint` true.
- A **renditions-only** photo shows **no broken legacy print button** (gated on `hasLegacyPrint`).
- `parse_variant` passes every category incl. near-miss → screen and over-length/weird → slug.
- Any rendition (incl. HEIC/video/pdf) enqueues **zero** `media_jobs`; lone print → promoted; in-batch
  `(screen, variant_key)` collision with different bytes → promoted (not dropped); re-dump same key →
  idempotent.
- **Pre-existing rollback bug fixed**: dedup re-upload + rollback does NOT unlink the original's file;
  `finalize`'s `moved_out` is optional and `routers/uploads.py:113` + the two tests compile unchanged.
- Two-screens-same-base → both visible, print binds first; deduped-screen → rendition lands on the dedup
  target.
- Delete photo → rendition rows + unique files gone, siblings untouched; legacy `print_path`/`POST …/print`/
  `print` tier unchanged.
- Each phase: pyright 0, ruff, pytest, `alembic check` no-op-after-create, regenerated openapi/schema, CI
  green all 4 jobs; prod + memos-old still 200.

## Risks / watch-items
- **Grouping is per-`finalize_pending` batch.** Prints added in a *later edit* (PATCH) than their screen
  (gap #5), or via the one-shot `POST /api/uploads/{uid}/finalize` / legacy `attachment_uids` path, are
  **promoted** (not grouped). Documented: *dump a screen and its prints together in one compose/edit.*
  Tested for the PATCH path.
- **HEIC/HEVC/video print stays zero-MediaJob** even through the sniffer (thermal cardinal rule).
- **Promoted-print cosmetics** — a promoted print keeps its `- print (…)` filename + gains derived tiers;
  intended, harmless.

## Rejected alternatives
- Every-file-an-Attachment + `WHERE role='screen'` filter (fragile hiding, thermal risk).
- Cross-batch base matching + orphan recovery + reaper (cut by decisions 2+4).
- Deterministic shared rendition paths + cross-row sha256 dedup (caused the overwrite/leak/shared-deletion
  HIGHs) — unique uuid filenames + write-once/promote instead.
- Backfill/drop `print_path` (cut by decision 3).
