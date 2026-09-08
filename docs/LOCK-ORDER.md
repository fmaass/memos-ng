# The row-lock order

Every transaction in memos-ng that takes more than one row lock takes them in one order. This file
is that order and the rules around it, as the code enforces them today. The reasoning, the cycle
analysis and the per-site derivation live in the design page, BookStack `/link/3515`
(LOCK-PROTOCOL-DESIGN.md, MEMNG-267); this is the operational half.

## The total order

    M  <  U  <  J  <  A  <  R  <  F        (E and D: isolated, never co-held — R11)

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

Within a class, acquire **ascending by canonical key, in ONE statement per class per
transaction**.

**J before A**, not A before J. Every J/A path in the tree acquires that way already —
`media.guard_publish`, `reclaim.reclaim_attachment` and its synchronous twin, and
`worker.recover_stale_media_claims` — so the direction costs zero reversals, where the other one
would cost four. It is also what keeps MEMNG-41 closed: with A taken first, a delete would have to
wait behind a job row it deliberately skips. Reversing **only** reclaim or **only** worker
publication manufactures a new cycle; they change together or not at all.

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
  delete having won.
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
  direction. F is acquired only after A and the frame worker reads A/M unlocked, so F sits last
  and never inverts.

This is not a claim that all application writes are deadlock-free. Unique-key arbitration on
`tags` is a separate class with its own settlement and is out of the protocol's scope.

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
