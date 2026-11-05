# The row-lock order

Every transaction in memos-ng that takes more than one row lock takes them in one order. This file
is that order and the rules around it, as the code enforces them today. The reasoning, the cycle
analysis and the per-site derivation live in the design page, BookStack `/link/3515`
(LOCK-PROTOCOL-DESIGN.md, MEMNG-267); this is the operational half.

## The total order

    M  <  U  <  J  <  A  <  R  <  F        (E and D: isolated, never co-held — R11)
              J  <  A  <  C                (C: explicitly one row, after A's KEY SHARE; implicitly,
                                            reclaim's J → A then MANY C rows via SET NULL /
                                            CASCADE; never with U/R/F, E or D — R11)
    M  <  C                                (the inclusion route only: M first, then the
                                            requested exclusion rows — R11)
    C  <  P                                (the collection delete: its ONE C row FOR
                                            UPDATE, then that collection's live P rows
                                            ascending in one statement; a trip filing part:
                                            its album's ONE C row, then the implicit KEY SHARE
                                            on its P and device rows; every other P holder
                                            holds one P row and nothing else — trip pass P1)
    H  <  M  <  U  <  A  <  C  <  P           (a trip filing part, the one transaction that
                                            meets all three of pass, collection and memo rows —
                                            MEMNG-1037; see H)

| | Table | Canonical key |
|---|---|---|
| M | `memos` | `memos.id` |
| U | `uploads` | `uploads.upload_uid` |
| J | `media_jobs` | `media_jobs.id` |
| A | `attachments` | `attachments.id` |
| R | `attachment_renditions` | `attachment_renditions.id` |
| F | `frame_share_jobs` | `frame_share_jobs.id` |
| E | `export_settings` | singleton |
| D | digest settings / runs | singleton |
| C | `collections` (+ `collection_exclusions`, `collection_members`, `collection_covers`) | `collections.id`; `collection_covers` by `(kind, key)` — after A, never before; one row when taken explicitly (`update_collection`, `collection_covers.set_cover`, `add_member` / `remove_member`, `passes._tag_trip_if_untagged` when a trip's pass is issued), possibly many when taken implicitly by an attachment delete's SET NULL / CASCADE (R11) |
| P | `passes` (+ `pass_devices`, `pass_events`, `pass_sync_state`) | `passes.id` — ONE row, FOR UPDATE, by `passes._lock_pass`; every pass change (edit, revoke, device revoke) takes it before writing the pass, its devices, its events or its sync row, and the transaction holds nothing of another class. The moderation verbs never hold it: they read the pass, end that transaction, and then write exclusions / member rows / reclaims exactly as the collection and attachment-delete paths do. `collections.delete_collection` is the one path that co-holds: the collection row FOR UPDATE (which also blocks an issue racing it — a trip's issue first takes that row FOR UPDATE in `passes._tag_trip_if_untagged`, re-reads it and gives an untagged trip its tag (MEMNG-1057), and an album's issue insert takes KEY SHARE on it), then `passes.revoke_collection_passes` takes that collection's live passes ascending in ONE statement, revokes each, and the delete's SET NULL touches the same rows again. So the only order the two meet in is C → P (a trip's issue too: the collection row, then only the new pass's own rows), and nothing holding P reaches for C |
| G | `guest_uploads`, `ingest_batches` — the ingest roles' own tables (trip pass P2) | Only the ingest roles lock them, and those processes lock nothing of M/U/J/A/R/F/E/D/C. Order: the pass row (P, FOR UPDATE to change it or reserve its bytes, FOR SHARE to re-check it), then its `pass_devices` row (FOR UPDATE to enroll, count or complete, FOR SHARE to re-check), then that device's `guest_uploads` rows ascending by `upload_uid` in ONE statement, then — only on the home interface, never together with a guest lock — one `ingest_batches` row by `uid` (`_transition`), or the purge's eligible batches ascending by `id` in ONE statement, re-checked by Postgres under the lock so a quarantine that landed meanwhile drops out. A completion holds the device row FOR UPDATE for its whole transaction, which is what makes overlapping completions of the same uploads JOIN. The advisory locks come after every row lock: the disk-reservation lock (upload create only), then a completion's per-hash blob locks, all taken up front in ascending hash order, then the change cursor's global one, last |
| T | `pats` — a device token and its capture grant (MEMNG-938) | `pats.id` — ONE row, FOR UPDATE, by `services/pats.mint_capture_grant`: the parent token, then the DELETE of its old grant and the INSERT of the new one. Isolated: that transaction holds nothing of any other class, and nothing that holds another class locks `pats` (R11) |
| H | home's `ingest_batches` (+ `ingest_blobs`, `trip_ingest_cursor`) — the trip ingest puller's ledger (trip pass P3a) | `ingest_batches.id` — ONE row, FOR UPDATE, by `trip_ingest._file_part`, once per filing part, each part its own transaction (and `_ack` / `_quarantine` / `_refuse_open`, and `_filing_committed`, which takes it alone in a fresh transaction after an interrupted filing COMMIT, to wait that transaction out and read its outcome); only the worker's puller touches these tables at home, one tick at a time under a transaction-scoped advisory lock. A filing part takes its batch row FIRST and then runs the member create path (`passes.create_guest_memo` → `memos.create_memo`; a later part `passes.append_guest_files`, which locks the first part's memo row FIRST like every memo edit, then finalizes with that memo's attachments in pass 0's one ascending acquisition), so H sits before every class that path takes and nothing holding those classes reaches for H. The pass is touched only IMPLICITLY, and only LAST: setting `memos.pass_id` and `attachments.contributed_by_device_id` takes KEY SHARE on the pass and device rows through their foreign keys, so an album's member rows (`collections.add_member`: A KEY SHARE, then the album row C) are written before that link (`passes._file_into_album`). Taken the other way round, the part held P (KEY SHARE) while it asked for C and the collection delete held C while it asked for P FOR UPDATE — a P→C / C→P cycle (security gate r1 #5). Every writer of these rows, in its order: filing part H → M → U → A → C → P(KEY SHARE); collection delete C → P (FOR UPDATE) → its member rows by CASCADE; album edit A(cover, KEY SHARE) → C; member add/remove A(KEY SHARE) → C; pass edit/revoke/device revoke P (FOR UPDATE) alone; album pass issue C(KEY SHARE via the insert's FK), trip pass issue C (FOR UPDATE, `_tag_trip_if_untagged`) → the new P; moderation never holds P (it ends the read first, then runs the member paths); memo delete M → A → C (CASCADE); the ingest host's purge locks only its own G rows on its own database. `tests/test_trip_filing_lock_order.py` runs every pair that can meet both ways round, one actor parked holding its locks, and asserts no 40P01 and that the waiter queues at the class this order puts first. Every puller transaction that can write the device or sync rows of more than one pass (`push_passes`, `push_device_revocations`, `pull_devices`, a `pull_batches` page) takes the passes it names first, ascending in ONE statement (`_lock_passes`), and only then their device or sync rows — the P order of every owner path; a refusal of one open batch writes one sync row and no other. The puller never holds P FOR UPDATE while filing |

`uploads.finalize_committed` (MEMNG-1167) takes ONE `uploads` row (U), FOR UPDATE, in a fresh
transaction of its own that locks nothing else and only reads after it: the finalize route asks it
whether an interrupted commit landed, and the lock is how it waits for that transaction to end.
The route closes its own session first, so it holds nothing the wait could cycle with.

Within a class, acquire **ascending by canonical key, in ONE statement per class per
transaction**.

**J before A**, not A before J. Every J/A path in the tree acquires that way already —
`media.guard_publish`, `reclaim.reclaim_attachment` and its synchronous twin, and
`worker.recover_stale_media_claims` — so the direction costs zero reversals, where the other one
would cost four. It is also what keeps MEMNG-41 closed: with A taken first, a delete would have to
wait behind a job row it deliberately skips. Reversing **only** reclaim or **only** worker
publication manufactures a new cycle; they change together or not at all.
`sibling._write_result` (MEMNG-796) acquires in the same order — its own job row, then its
attachment, each `FOR UPDATE` — and `tests/test_sibling_job.py` races it against
`reclaim_attachment` on two connections (the attachment-first order there is a 40P01).
After its attachment row, a reclaim takes one transaction-scoped advisory lock per path the row
names (`reclaim._named_elsewhere`, MEMNG-959), in sorted path order, LAST — nothing else takes
them, and the holder asks for no row after them — so two reclaims of rows naming one file queue on
the path instead of each keeping it for the other.

`uploads.upload_uid` is a text PK and text ordering is collation-dependent. The rule is therefore
that every path uses the identical `ORDER BY Upload.upload_uid` clause against the one database —
the protocol needs the orders to *agree*, not to be collation-independent.

Placing J between U and A is free: no path holds U and J together, and every J enqueue is an
INSERT of a private row, which is not an acquisition (R8).

## The rules

- **R1 — the no-memo rule.** Standalone finalize, append, the capture GET, the interactive
  rendition attach and the multipart `/print` start at U (or at A) and **never acquire M**.
  `finalize()` and `finalize_rendition()` must not take a memo lock. A path that discovers it
  needs a parent memo **releases everything (rollback) and restarts from M**; it may not climb.
- **R2 — one globally sorted set per class.** A transaction acquires each class **once**, in one
  statement, over the complete set it will need. In `link_attachments` that is a single
  `WHERE (A.uid IN :uids AND A.owner_id = :owner) OR (A.memo_id = :memo_id)`, `ORDER BY A.id
  FOR UPDATE`, `populate_existing=True`, partitioned in Python afterwards — the OR, not a single
  owner conjunct, because the memo-linked set carries no owner predicate and narrowing it would
  silently drop a legacy or foreign-owner row from the renumber.
- **R3 — hash before you lock A (batch finalize).** `finalize_pending` runs a **pass 0**: acquire
  the whole U set in one `ORDER BY upload_uid FOR UPDATE`; under those locks stat, sniff,
  `ensure_capture` and `sha256` each upload; resolve dedup candidates with an *unlocked* read;
  acquire the whole A set in one `ORDER BY A.id FOR UPDATE` with `populate_existing=True`;
  **revalidate every candidate under its lock** (`owner_id`, `memo_id IS NULL`), dropping any that
  no longer qualifies. After pass 0 the per-upload loop acquires no further row locks.
  That one A acquisition is `_lock_attachments`, and `acquire_attachment_set` asks for it on
  behalf of a caller that finalizes nothing: a PATCH which only re-declares a memo's picture
  list still links, renumbers and detaches attachments, so it takes A here, in this order,
  rather than where it writes those rows.
- **R4 — U covers every upload the transaction will delete.** The poster, the rendition no-ops,
  the rendition finalize and the sibling bind all delete rows this transaction already holds, so
  none of them can wait after A is held.
- **R4a — one acquisition, two continuations.** Pass 0 locks the *union*
  `upload_uids ∪ video_posters.values()` in ONE statement and then **partitions the result by
  role**. An absent or incomplete `upload_uids` member is `UploadsUnlinkable` → **409**
  `uploads_unlinkable`; an absent or invalid `video_posters` value is `VideoPostersInvalid` →
  **422** with `type = video_poster.<reason>`. No status code and no error body changes on any
  path.
- **R5 — the interactive rendition attach reorders to U → A.** Unlocked read of the attachment for
  the ownership/mime checks first (so the error precedence the API has today does not change),
  then the Upload `FOR UPDATE` owner-scoped → 404 if gone, then the attachment
  `FOR UPDATE OF Attachment … populate_existing` re-checking owner → 404 if it vanished.
- **R6 — memo DELETE takes M first.** The ORM's default (no `passive_deletes`, FK `SET NULL`)
  emits `UPDATE attachments SET memo_id=NULL` **before** `DELETE FROM memos`, i.e. A before M. The
  DELETE route therefore calls `lock_memo_for_write` at the head of its transaction. Do **not**
  add `passive_deletes=True`: `delete_memo` reads `memo.attachments` to build the orphan journal,
  and the SET-NULL orphan **is** the durable journal.
- **R6a — a locking read that finds no row is a 404, never a 500.** A lock acquisition may lose
  its row, and losing it is an ordinary answer. `lock_memo_for_write` returns `Memo | None`. On
  PATCH a `None` is `HTTPException(404, "Memo not found")`, raised inside `attempt()` **before**
  `record_edit_key`, so no edit key is consumed and no bytes have moved. On DELETE a `None` is the
  same 404 — the concurrent delete won. On create a `None` is impossible (the row is this
  transaction's own insert) and stays loud: asserted, never silently skipped. This is the contract
  every other lock already honours — `ensure_capture` returns `None` for a vanished upload,
  `reclaim_attachment` answers `"vanished"`, `guard_publish` treats a missing job row as the
  delete having won. `withdraw` (`DELETE /api/uploads/{uid}`, MEMNG-953) takes ONE U row, owner
  scoped, and nothing else in its transaction; a `None` after the wait is a finalize that won, and
  the route answers its idempotent 204 with nothing changed. Every consuming path re-reads
  `withdrawn_at` on the U row it already holds (`finalize`, `append_stream.relock`) or filters it
  in its one ascending U statement (`finalize_pending` pass 0), so no lock is added to the order.
- **R7 — J → A is the protocol.** `SKIP LOCKED` stays where it is (reclaim's job sweep, the claim,
  the recovery sweeps): it is a *queue* mechanism there, never a way to omit a requested row.
- **R7a — a sweep that touches many attachments acquires them as one ascending set.**
  `recover_stale_media_claims` selects its stale J set with `.order_by(MediaJob.id)`, then
  **before the loop** computes the terminal attachment set and acquires it in ONE
  `ORDER BY Attachment.id … FOR UPDATE`. The precise set, not every attachment named by a stale
  job: a job that merely re-queues writes no attachment row. J is still acquired before A.
- **R8 — a fresh private row is not an acquisition.** Rows this transaction inserted are invisible
  to every other transaction, so they carry no order. They participate only through unique
  indexes, and each such index keeps its existing settlement (rendition savepoint no-op, edit key
  recorded before any bytes move, the create replay loop).
- **R9 — modes and freshness are preserved verbatim.** FOR SHARE stays FOR SHARE and `of=Memo`
  stays; no share→exclusive upgrade is introduced on a replay path; every memo lock keeps
  `populate_existing`; both `print_path` refreshes stay locking refreshes.
- **R10 — every value read before a lock wait is re-read after it.** The A acquisition of pass 0
  carries `populate_existing=True`, so the revalidation judges the locked row rather than the
  stale copy already in the identity map.
- **R11 — isolated domains.** No path may hold E or D together with any of M/U/J/A/R/F, in either
  direction, and no path may hold C together with U/J/R/F, E or D explicitly, nor with M except in
  the one inclusion operation below. C's explicit neighbours are A, with the edge **A < C**, and M
  for that operation, with the edge **M < C**. Reclaim (`reclaim.reclaim_attachment`) acquires
  **J → A** — its media-job rows (`app/services/reclaim.py:258-266`), then the attachment row
  (`:278-283`) — and deleting that row then waits IMPLICITLY on C through the FKs: ON DELETE SET
  NULL writes EVERY collection whose committed cover is that attachment, and ON DELETE CASCADE
  removes every `collection_exclusions` row naming it — possibly many C rows, not one. So the
  order is **J < A < C**, with C possibly several rows on the implicit side, and every path that
  holds A and C takes A first.
  `collections.update_collection` (and `create_collection`, which holds no C row) takes FOR KEY
  SHARE on the cover attachment's row (`collections._hold_cover`) BEFORE its one FOR UPDATE on
  the collection row, and nothing else; a cover deleted before it could be held is `CoverGone`,
  never a foreign-key violation at flush. `collection_covers.set_cover` (MEMNG-853, the chosen
  cover of a tag, month or year collection) takes the same `_hold_cover` FOR KEY SHARE and then
  writes its ONE `collection_covers` row by upsert; `clear_cover` deletes one such row and takes
  no A. An attachment delete reaches every cover row naming it through ON DELETE CASCADE, after A,
  so the edge is again **A < C**. An album's member writes (MEMNG-802) take the same shape:
  `collections.add_member` and `collections.remove_member` each take FOR KEY SHARE on the ONE
  picture's row (`_hold_member_attachment`, A) FIRST, then FOR NO KEY UPDATE on the ONE album row
  (`_lock_album`, C), then insert or delete the `collection_members` row and write the album's
  `updated_at` under that lock — one picture per transaction, so each class is still one row,
  and **A < C** again. Holding A first is what keeps an attachment delete (A FOR UPDATE, then the
  cover SET NULL and the member CASCADE, in either order) from meeting a member write in reverse.
  F is acquired only after A and the frame worker reads
  A/M unlocked, so F sits last and never inverts.
  **The one M–C operation (MEMNG-785):** `POST /api/collections/{uid}/attachments`
  (`routers.collections._include_from_memo`) runs one transaction per memo: `lock_memo_for_write`
  (M) FIRST, then the memo's content and `memo_tags` rebuild, then ONE `DELETE` of the requested
  attachments' `collection_exclusions` rows of that collection (C, `include_attachments`), then
  commit — **M then C, never C then M**. It takes no A, J, U, R or F lock. No other path holds C
  and then takes M: the collection routes (`update_collection`, `create_collection`,
  `delete_collection`, `exclude`, `include`) take no memo lock, and reclaim's implicit C rows
  follow J → A with no M (an attachment delete does not lock its parent memo row). So the edge
  adds no cycle. The lock orders the append against edits that COMMITTED before it was granted;
  it cannot stop a later full-content PATCH from a client holding older text.

This is not a claim that all application writes are deadlock-free. Unique-key arbitration on
`tags` is a separate class with its own settlement and is out of the protocol's scope: a missing
tag is created by `INSERT … ON CONFLICT (name) DO NOTHING` and re-read, missing names in sorted
order (`memos._get_or_create_tags`, MEMNG-785).

## What the protocol does not change

The exclusions below are binding. Each is copied from the design's §3.2 in its own words; the
citations are given as file and function rather than as line numbers, so they stay true as the
files move.

- **X1** Do **not** sort processing semantics with lock order. Acquisition is canonical; processing
  keeps declared attachment order, first-mention ranking, screen/print grouping and existing
  positions (`uploads.finalize_pending`'s passes; `memos.link_attachments`' rank and append rules).
- **X2** Do **not** fix dedup contention by accepting a stale candidate or stealing a linked
  attachment. Revalidate owner and `memo_id IS NULL` after the wait; keep the linking steal guard
  (`uploads.finalize_pending`'s pass-0 revalidation and `uploads.finalize`'s standalone locked
  re-read; `memos.link_attachments`' `att.memo_id is not None and att.memo_id != memo.id`).
- **X3** Do **not** drop freshness when consolidating locks. `populate_existing` on the memo lock
  (`memos.lock_memo_for_write`), the rendition attach's read-through-the-lock
  (`assets.attach_rendition`), and both `print_path` refreshes arbitrating the displaced path
  (`assets.upload_print`, `uploads.bind_print_to_sibling`) all stay.
- **X4** Do **not** validate consumed uploads before idempotency replay. Replay wins first on both
  create and PATCH (`memos.get_by_idempotency_key`, `memos.get_memo_by_edit_key`, both reached
  before any upload work). Keep the SHARE reads and `OF Memo`; introduce no shared→exclusive
  upgrade on a replay path.
- **X5** Do **not** use `SKIP LOCKED` to silently omit a requested upload or attachment. A refused
  batch keeps its media and reports the existing missing/incomplete or poster error
  (`uploads.UploadsUnlinkable` → 409 `uploads_unlinkable`; `uploads.VideoPostersInvalid` → 422
  `video_poster.<reason>`).
- **X6** Do **not** remove reclaim's revalidation, file journal or non-blocking job cleanup. Keep
  the identity-vs-orphan predicates, the per-attachment transaction boundary, and the absence of a
  media-job FK cascade (`reclaim._ineligible`, `reclaim.reclaim_attachment`,
  `asset_paths.all_asset_paths`; `models.MediaJob`).
- **X7** Do **not** unlink consumed or expired parts before commit, and do **not** discard
  committed media in a cleanup path. Keep `consumed_out`, `TrackedMove`, poster-copy settlement and
  the committed-state guards (`uploads.PosterCopy`, `storage.TrackedMove`, the create/PATCH routes'
  post-commit reaps, and the expiry sweep's `uploads.reap_expired_committed`, which unlinks a part
  only after the deletion of its row committed). `tests/test_commit_race_reconcile.py` documents
  actual damage, not a hypothetical.
- **X8** Do **not** unify ingest and interactive replacement semantics. Ingest renditions are
  write-once; interactive renditions replace; a sibling print collision promotes rather than
  dropping a master (`uploads.finalize_rendition`, `assets.attach_rendition`,
  `uploads.bind_print_to_sibling`).
- **X9** Do **not** reverse only reclaim or only worker publication. All J/A participants —
  publication, stale recovery, `_fail_attachment`'s implicit terminal update, both reclaims —
  agree already, and they change together or not at all (`media.guard_publish`,
  `worker._fail_attachment`, `worker.recover_stale_media_claims`, `reclaim.reclaim_attachment`).

## The transaction / I-O boundary

> A transaction may hold row locks across **bounded local filesystem I/O under `DATA_ROOT`** —
> `stat`, sniff, `sha256`, `os.replace`, `copy`, `unlink`. It may **never** hold a row lock across
> (a) a network call to a third party (SMTP, Dropbox, the frame album API), (b) a subprocess
> derivation (ffmpeg / pyvips / a transcode), or (c) a client-driven stream (a request body being
> received).

All three prohibitions are honoured today and must stay honoured: `worker.run_once` commits the
`processing` claim before `media.process_job`; `media.process_job` commits the read transaction
before the derivation; the frame worker commits the `claimed` tombstone before the album upload;
`append_stream` spools the request body **before** taking the U row lock.

The window is deliberately bounded rather than replaced by a durable claim/generation scheme,
because the memo and upload work share one transaction that may still roll back — `moved_out` /
`TrackedMove`, `consumed_out` and `PosterCopy` all exist for that rollback, and each extra commit
boundary turns one of them into a durable-orphan problem needing its own reconciliation
(MEMNG-146 pins the damage a single ambiguous commit already did). Releasing A before publication
would re-open the race `guard_publish` closes: a transcode finishing after a delete would
`os.replace` its output back into place.

Pass 0 lengthens the U-hold window (every upload of a batch is hashed before any A is taken) and
*shortens* the A-hold window (A is acquired after all hashing, not interleaved with it) — less
time holding the contended class.

Commit `2e3b89c`'s blanket no-I-O-under-lock rule remains what it is, a **digest-subsystem** rule,
and is not retro-applied here.

## The census

`docs/lock-census.tsv` names every EXPLICIT lock site in `app/` and `scripts/`: one row per site,
ordered by path then line, with the enclosing `def` and the mode (`for_update` for a
`.with_for_update(...)` acquisition, `refresh` for a `refresh(..., with_for_update=True)` locking
re-read). Adding a lock site is therefore a diff to a reviewed file, not a line nobody notices.

    uv run pytest tests/test_lock_census.py            # the gate
    uv run python -m tests.test_lock_census > docs/lock-census.tsv   # regenerate, never by hand

The check compares **multiplicities**, not sets: a second acquisition inside an already-listed
function is caught. It carries a positive control (the scan must find
`app/services/memos.py::lock_memo_for_write` and `app/services/reclaim.py::reclaim_attachment`, so
a scanner that has stopped parsing fails saying so rather than reporting a clean tree) and a
mutation control (a throwaway module with one lock in it, planted on the scan path, must come back
as an uncensused site).

**Limitation, stated rather than implied:** this covers explicit acquisitions only. Implicit
writers — UPDATE, DELETE, autoflush, FK and unique arbitration all wait on rows too — are covered
by review and by the concurrency probes, not by any scan.

## The probes and their mutation controls

`tests/test_lock_protocol_concurrency.py` holds the deterministic two-actor probes: two database
sessions on one event loop, each running a real route or service function, one PARKED inside its
transaction while the other is proved through `pg_stat_activity` — never through a sleep — to be
waiting on a row lock. Most of them deadlock with SQLSTATE `40P01` on the tree that predates the
fix they pin.

Two probes cannot be red on a prior tree, because they pin a cycle that tree does not have. Their
negative control is a named patch carried in `tests/mutations/`, applied by hand and enabled by
the environment variable the probe's `skipif` names:

| Patch | Environment | What it removes |
|---|---|---|
| `tests/mutations/no_r7a.patch` | `MEMNG267_MUTATION=no_r7a` | R7a's pre-loop attachment acquisition in the recovery sweep |
| `tests/mutations/no_named_arm.patch` | `MEMNG267_MUTATION=no_named_arm` | the named-attachment arm of pass 0 |
| `tests/mutations/c5b_merge_answers.patch` | `MEMNG267_MUTATION=c5b_merge_answers` | the split between the 409 and 422 continuations |

    git apply tests/mutations/no_r7a.patch
    MEMNG267_MUTATION=no_r7a uv run pytest tests/test_lock_protocol_concurrency.py
    git apply -R tests/mutations/no_r7a.patch

The suite itself never applies one: it must stay green on the tree it is run against.

`scripts/check-mutations.sh` runs that loop for every row of `tests/mutations/expected.tsv`
(`patch`, `marker`, `expected_failed`, optional `sqlstate`). It applies each patch in place with
`git apply`, runs the concurrency module with `MEMNG267_MUTATION` set to the row's marker,
reverses the patch and proves the revert by sha256; it refuses to start if the file a row's patch
touches has uncommitted changes. A row passes only when pytest exits 1 and the set of FAILED node
ids — read from the run's JUnit report, not from the terminal output, and cross-checked against
pytest's own summary line — equals the row's `expected_failed` exactly, with zero errors; an
errored probe never counts as a failing one, and a named `sqlstate` must appear in the run's log.
Before the loop the module is run once with `MEMNG267_MUTATION` removed from the environment as
the positive control, which must exit 0. `--selftest` proves the checker can fail. It takes the
database from the caller's environment and neither creates, migrates nor drops one. **It MUTATES
the working tree it runs in**, so nothing else may read or write that checkout while it runs.
