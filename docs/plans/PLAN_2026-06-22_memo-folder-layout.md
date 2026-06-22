# PLAN — Human-browsable per-memo originals layout

Status: **DRAFT — review before implementing.** No code yet.
**Rev 7 — scope cut to "new writes only".** The owner decided the new layout applies ONLY to
newly-written masters; existing masters never move and there is **no migration**. This deletes the
entire rev-6 safety machinery — the 661-file migration, the reference-GC, the
`pg_advisory_xact_lock(GC_KEY)` invariant, and the worker ENOENT-retry are all **gone**. What
remains is a single atomic-move placement step that is safe by construction.

## Owner decisions (confirmed this session)
1. **New writes only.** Only newly-uploaded masters land in the per-memo folder. Existing masters
   stay exactly where they are; the owner will re-upload memos over time, which migrates them
   naturally. No migration script.
2. **Folder layout** = `originals/YYYY/MM/YYYY-MM-DD_HHMM_<memo-uid>/` (year/month parents reuse the
   existing `originals/YYYY/MM/` tree; per-memo folder name carries the full timestamp + uid).
   Date components come from the memo's display `created_at`.
3. **Date edit does NOT move files.** The folder name reflects the creation timestamp at first
   placement and never moves; the uid keeps it unique. Editing a memo's display date leaves the
   folder where it is. (Keeps the live path move-free.)
4. Real filenames inside the folder; in-folder collisions get ` (n)`; an attachment finalized but
   never linked to a memo stays in `originals/_unfiled/`. Derived web tiers (`assets/`) are
   untouched.

## Current state (grounded in code)
- `finalize` (`app/services/uploads.py:209`) writes the screen master to
  `originals/{now:%Y}/{now:%m}/{uuid}_{name}` using the **upload** time, and runs **before the memo
  exists** (upload is a separate step from create/patch). It appends the moved path to `moved_out`
  (new-file branch only) for rollback-unlink.
- `finalize_rendition` (`uploads.py:278`) writes the print master to `print/{uuid}{ext}`, no
  MediaJob.
- `link_attachments` (`app/services/memos.py:77`) is the SOLE place an attachment gets `memo_id`,
  called by `create_memo` (service, :72) and `patch_memo` (router, :270). At call time `memo.uid`
  and `memo.created_at` are set (flushed); the attachment rows + their renditions are in-session.
- The router create/patch flow already owns a `moved: list[str]` and a `try/finally` that
  `_unlink_paths(moved)` on rollback (`memos.py:208-229`, `:261-275`). `_unlink_paths`
  (`memos.py:23`) is `missing_ok` + `OSError`-suppressed.
- Derived tiers serve by uid from `assets/`; only `original`/`print`/rendition serve from the DB
  `storage_path`. Relocating a master = a file move + a `storage_path` update. No serve-path or
  signed-URL change.

## Why this is safe by construction (no GC / no lock / no migration)
A newly-finalized master is written to a staging bucket `originals/_unfiled/{uuid}_{name}` — a
**brand-new file owned by the in-flight request**. When the memo is saved, `link_attachments`
**atomically moves** (`os.replace`) it into the memo folder and updates `storage_path`, in the SAME
transaction that commits the memo + the `MediaJob`. Therefore:
- **No committed master is ever relocated or deleted on the live path.** Existing masters (old flat
  layout) are never touched — placement only ever acts on a `_unfiled/` source. There is nothing for
  a GC or advisory lock to protect.
- **No worker race.** The worker reads `storage_path` only from committed `MediaJob` rows; placement
  updates `storage_path` and the job commits together, so the worker never observes a pre-placement
  path. (The rev-6 ENOENT-retry is unnecessary.)
- **Rollback-safe.** The memo-folder destination is appended to the existing `moved` list, so a
  rolled-back post unlinks it; the now-stale `_unfiled` entry is a harmless `missing_ok` no-op. The
  Attachment/MediaJob rows roll back too, so no DB row references the discarded bytes.
- **Idempotent placement guard.** `link_attachments` places a master ONLY if its current path is
  under `_unfiled/`. An attachment already in a memo folder or the legacy flat layout (e.g. the
  back-compat `attachment_uids`, or an attachment re-linked between memos) is **skipped** — this is
  exactly what enforces "new writes only / never move an existing master".

## Design

### `app/services/storage.py` (new — pure, fully unit-tested)
- `memo_folder_name(created_at, uid) -> str` → `f"{created_at:%Y-%m-%d_%H%M}_{uid}"`.
- `memo_dir(data_root, created_at, uid) -> Path` →
  `data_root/"originals"/f"{created_at:%Y}"/f"{created_at:%m}"/memo_folder_name(...)`.
- `sanitize_name(filename) -> str` → `Path(filename).name`, strip path separators + control chars,
  cap length (e.g. 200), fall back to a uuid-based name if empty.
- `unfiled_dir(data_root) -> Path` → `data_root/"originals"/"_unfiled"`.
- `is_unfiled(path, data_root) -> bool` → the placement guard (parent == `_unfiled`).
- `place_master(src: Path, dest_dir: Path, name: str) -> Path` (off-loop via `to_thread`): `mkdir`
  parents; reserve `dest_dir/<sanitized>` with `os.open(..., O_CREAT|O_EXCL)` (bump to ` (2)`,
  ` (3)` on `FileExistsError` — unique name under concurrency, no overwrite); `os.replace(src,
  reserved)`; return `reserved`.

### Wiring
- **`finalize`** (`uploads.py`): write the original to `originals/_unfiled/{uuid}_{name}` (was
  `originals/YYYY/MM/...`); keep appending to `moved_out`. (`os.replace` from the `.part` — a
  brand-new file.)
- **`finalize_rendition`** (`uploads.py`): write the print master to
  `originals/_unfiled/{uuid}{ext}` (was `print/...`); still no MediaJob; keep `moved_out` append.
- **`link_attachments`** (`memos.py`, gains `moved_out: list[str] | None = None`): after setting
  `att.memo_id`, for each newly-linked attachment whose `storage_path` `is_unfiled`: compute
  `dest = memo_dir(data_root, memo.created_at, memo.uid)`; `place_master` the screen master, set
  `att.storage_path`, append the new path to `moved_out`. Then **explicitly query** the attachment's
  renditions (`select(AttachmentRendition).where(attachment_id == att.id)` — avoid async lazy-load
  surprises) and `place_master` each that `is_unfiled`, updating `rendition.storage_path`. Legacy
  `att.print_path` that `is_unfiled` is placed too.
- **`create_memo`** (service, gains `moved_out=None`) threads it into `link_attachments`;
  **router `create_memo`** passes its `moved` list through.
- **`patch_memo`** (router): pass `moved` into the existing `svc.link_attachments(...)` call.
- **`upload_print`** (`assets.py`): if the target attachment is linked to a memo, `place_master` the
  freshly-uploaded print into that memo's folder (it is new bytes → safe) and set `print_path`
  there; otherwise keep today's `print/` path. Small, keeps a re-print self-contained.

### Untouched (confirmed)
Serve paths, signed URLs, `assets/` tiers, `_all_asset_paths` deletion (reads `storage_path` from
the DB, layout-agnostic), the worker, and all existing masters in the old flat layout.

## Phases (each closes with the local gate; P1 + P2 each end with `/converge`)
- **P1 — placement core + screen/rendition placement on link.** `app/services/storage.py`;
  `finalize`/`finalize_rendition` → `_unfiled`; `link_attachments` placement (screen + renditions)
  with the `_unfiled` guard + `moved_out` threading; `create_memo`/`patch_memo` wiring. Tests below.
- **P2 — legacy print_path placement.** `upload_print` places into the memo folder when linked.
  Tests: a `/print` upload to a memo lands beside its screen; an unlinked attachment keeps `print/`.

(No P3. There is no migration.)

## Tests (real — fail when the feature is broken)
- **`tests/test_storage.py`** (pure): `memo_folder_name` format; `memo_dir` path; `sanitize_name`
  strips `/`, `\`, control chars, over-length, empty→fallback; `place_master` reserves ` (2)` on a
  name collision and never overwrites an existing file; `is_unfiled` true only for the staging dir.
- **`tests/test_variant_ingest.py` / new placement test**: posting a memo with
  `X - screen.jpg` + `X - print (glossy).png` lands the screen at
  `originals/<YYYY>/<MM>/<YYYY-MM-DD_HHMM>_<uid>/X.jpg` and the rendition at `…/X - print
  (glossy).png` under their real names, and both still serve.
- An attachment finalized but never linked stays in `originals/_unfiled/`.
- Two photos with the same real filename in one memo → second gets ` (2)`.
- **Rollback**: force a post to roll back after placement; assert the placed file is unlinked AND no
  pre-existing master was touched (placement only acted on `_unfiled`).
- **Guard / no-move-existing**: link an attachment whose `storage_path` is already a memo folder (or
  the legacy flat layout) → it is NOT moved (proves "new writes only").
- **Worker sees the final path**: after commit, the `MediaJob`'s attachment `storage_path` is the
  memo-folder path (no `_unfiled` leakage into a committed job).
- Frontend: unchanged (server builds URLs); existing specs stay green.

## Acceptance Criteria
- A posted `X - screen.jpg` + `X - print (glossy).png` land in
  `originals/YYYY/MM/YYYY-MM-DD_HHMM_<uid>/` under real names; photo + print + web tiers all serve.
- Existing masters are **never moved**; an attachment already outside `_unfiled` is left in place.
- Collisions get ` (n)` via `O_EXCL`; path separators / control chars in a dumped name are sanitized.
- A rollback after placement unlinks the placed file and touches no pre-existing master.
- Editing a memo's display date does NOT move its folder or files.
- Legacy `/print` upload to a linked memo lands in the memo folder.
- Each phase: pyright 0, ruff, pytest, alembic no-op (no schema change), regen openapi (no route
  change → no drift), CI green 4 jobs; prod + memos-old 200.

## Risks / watch-items
- **`created_at` availability at link time** — set + flushed before `link_attachments` (verified
  `memos.py:71-72`); for create with a custom `created_at` it's applied at `services/memos.py:67`.
- **Renditions loaded in async** — query them explicitly in `link_attachments` rather than relying on
  lazy access.
- **Stale `_unfiled` entry in `moved`** — harmless (`_unlink_paths` is `missing_ok`); optionally
  replace rather than append.
- **Split layout per memo is impossible for a re-upload** but possible if an OLD-layout memo gets a
  NEW photo added via patch (new photo → new folder, old photos stay flat). Accepted: the owner is
  re-uploading whole memos; a mixed memo is cosmetic, not a correctness issue.

## Rejected / superseded
- **Rev-6 copy-then-GC + advisory-lock + 661-file migration** — superseded by the owner's
  "new writes only" decision. With no existing master ever relocated, the entire deletion-safety
  apparatus (GC, `GC_KEY`, importer retirement, ENOENT-retry) is unnecessary. A move of a brand-new
  `_unfiled` file is atomic and rollback-tracked; nothing is ever deleted on the live path.
- **Relocate-on-update** (consolidate an old memo's masters when touched) — rejected: it would move
  committed masters and so reintroduce the GC/lock machinery for marginal tidiness.
