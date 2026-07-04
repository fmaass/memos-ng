---
name: "Aura frame email share for #curated photos"
overview: "When a memo tagged #curated carries image attachments, memos-ng automatically emails each photo (resized JPEG, EXIF-preserving) to a configurable Aura frame address via iCloud SMTP, with a per-user settings section and a durable job queue. Central decisions: separate frame_share_jobs queue (not media_jobs), dedicated 2400px render (not the mobile tier), and an empirical Phase-0 SMTP/Aura probe before any code."
scope: standard
project: "memos-ng"
feature: "aura-frame-email"
status: draft
created: "2026-07-03"
todos:
  - id: "p0-empirical-probe"
    content: "Phase 0 — probe iCloud SMTP From rules + Aura sender auth end-to-end"
    status: done
  - id: "p1-backend-enqueue"
    content: "Phase 1 — config, migration (user cols + frame_share_jobs), enqueue on #curated"
    status: done
  - id: "p2-worker-send"
    content: "Phase 2 — worker render+send path, aiosmtplib, retries, EXIF preservation"
    status: done
  - id: "p3-settings-ui"
    content: "Phase 3 — settings API extension, OpenAPI regen, SettingsView section, test-send"
    status: done
  - id: "p4-deploy-acceptance"
    content: "Phase 4 — /safe-deploy + acceptance: photo visibly lands on the frame"
    status: done
isProject: false
---

# Implementation Plan: Aura frame email share for #curated photos

## Context for Independent Review

- **Current state:** memos-ng is the production Memos replacement (cutover to
  `memos.discomarder.live` completed 2026-06-21; latest release v1.1.0, commit `232e66d`).
  Backend is FastAPI + SQLAlchemy 2 async; a separate worker container (same image,
  `command: worker`) polls a Postgres `media_jobs` queue (`FOR UPDATE SKIP LOCKED`,
  `app/worker.py:63-81`) discriminated only by a 2-value thermal `lane` enum
  (`app/models.py:37-39`) and the attachment MIME (`app/services/media.py:245-279`).
  Image processing uses **Pillow + pillow-heif** (not pyvips, despite older docs):
  `process_image` (`app/services/media.py:40-65`) emits an 800px thumb and a 2048px
  "mobile" JPEG at quality 80, EXIF not preserved. Memo tags are parsed with
  `TAG_RE = r"#(\w+)"` in `parse_tags` (`app/services/memos.py:28-35`) on both
  `create_memo` (`app/services/memos.py:86`) and `update_memo`
  (`app/services/memos.py:267`); there is **no event/hook system** — the closest
  existing "after media work, notify" pattern is web push fired best-effort from the
  worker (`app/worker.py:112-125`, `app/services/push.py:38-67`). Per-user settings
  already exist as `User` columns (`default_visibility`, `theme`, `avatar` —
  `app/models.py:67-73`) exposed via `GET/PATCH /api/settings`
  (`app/routers/settings.py:34-58`) and edited in
  `frontend/src/views/SettingsView.vue` (TanStack Query composable
  `useSettings.ts`). **There is no email code or SMTP dependency anywhere in the
  repo** — this plan adds email sending from scratch.
- **Problem / motivation:** the user bought an Aura digital photo frame. Aura frames
  accept photos by email to a per-frame address (`…@send.auraframes.com`). Desired
  behavior: whenever a photo is uploaded to a memo tagged `#curated`, memos-ng emails
  it to the frame automatically — with the destination (send-to) and identity
  (send-from) configurable in a new settings section.
- **Researched external facts (2026-07-03, cited in the research fact sheets kept
  with the session):**
  - Aura email-to-frame accepts **one photo per email**; formats **JPG/PNG/TIFF only**
    (no HEIC, no video via email); email subject/body are ignored (no caption);
    photos sort by **EXIF DateTimeOriginal** in chronological mode; senders must be
    **registered frame members and send from the email address linked to their Aura
    account** — non-member mail is rejected with a notification. No official
    attachment size cap; Aura's web uploader caps at **9.5 MB/image** (best proxy).
    Current Aura panels top out at 1600×1200 (Walden/Aspen); the largest panel ever
    shipped is 2048×1536 (Mason Luxe).
  - iCloud SMTP: `smtp.mail.me.com:587` STARTTLS, auth = **full primary iCloud
    address + app-specific password** (requires 2FA; **all ASPs are revoked when the
    Apple ID password changes**). From may be the primary address or one of up to 3
    **regular** iCloud aliases. `honey.linens-45@icloud.com` matches Apple's **Hide
    My Email** format; HME addresses are a relay, not an owned SMTP identity —
    sending From an HME address over SMTP is expected to be **rejected** (55x-class),
    though no captured transcript confirms the exact code. iCloud limits: 1000
    messages/day, 20 MB/message — far above this feature's needs.
  - **User action 2026-07-03:** the user added **`fabianmaass@me.com`** as a member
    of the Aura frame. `@me.com` is a legacy iCloud identity Apple treats as an
    owned alias of the account, so it is a legitimate SMTP From candidate — this
    supersedes `honey.linens-45@icloud.com` as the planned sender and reduces the
    HME question to a curiosity probe.
- **Scope boundary — IN:** trigger on `#curated` (create and edit paths), per-user
  settings (send-to + send-from) with a new SettingsView section and a test-send
  action, durable `frame_share_jobs` queue processed by the existing worker, a
  dedicated EXIF-preserving JPEG render (longest edge 2400 px, quality ~88), retries
  with backoff, at-most-once-per-attachment idempotency.
  **OUT (explicit deferrals):** video sharing (Aura email path rejects video),
  captions (Aura ignores email text), multi-frame fan-out (one configured send-to;
  Aura needs one email per frame anyway), retroactive sending when settings are
  configured after upload, deleting photos from the frame, a global/server-level
  settings table, any UI to browse/retry the job queue beyond logs, and web-push
  failure notifications (structured logs + `failed` job state suffice for v1).
- **Hard constraints:** production system — branch discipline
  (`feature/aura-frame-email`, never commit to main), CI green on all jobs
  (backend, frontend, **api-drift** — the OpenAPI schema/`schema.d.ts` must be
  regenerated when `/api/settings` changes), local slim-image `docker build` before
  push because `pyproject.toml` gains `aiosmtplib` (python:3.13-slim has no
  compiler), off-loop blocking I/O via `from anyio import to_thread` +
  `to_thread.run_sync(...)` (never `import anyio` — pyright misresolution),
  security/logic-critical frontend behavior extracted into pure functions and
  unit-tested directly (happy-dom limits). SMTP credentials are secrets → new
  `Settings` env fields following the VAPID empty-default-disables pattern
  (`app/config.py:75-77`), never logged. **SMTP 250 is relay acceptance, not
  delivery** (LSN-0004/LSN-0008): the acceptance probe is the photo visibly on the
  frame, not a green SMTP session.
- **Terminology & decisions already locked:** ADR-0008 — HEIC originals are the
  archive master and are never modified (renders read the original, output JPEG).
  ADR-0009 — media serving via HMAC-signed URLs (untouched here). "Tiers" = derived
  assets (thumb/mobile/web); this plan's frame render is **not** a stored tier — it
  is generated at send time and attached, not persisted under `data/assets/`.
  Two other plans are active (1667 per-memo originals layout — P1+P2 merged on main,
  new-writes-only, not yet deployed; 1665 screen/print renditions); this plan works
  alongside them and must resolve original-file paths through the same helper the
  assets router uses (`app/routers/assets.py:31-39`), not hardcoded layout
  assumptions.
- **Hard-won constraints from prior sessions:** declaring "done" from an internal
  signal instead of a named acceptance probe on the real surface is the dominant
  recurring failure — hence Phase 4's frame-visible probe. Worker shares Saturn with
  the whole household stack; per-photo Pillow decode of a handful of curated photos
  a day is thermally negligible (the thermal rule targets transcode storms), but the
  render happens in the worker container, not the app container, keeping the app
  responsive.

## Goal

When a user uploads or edits a memo tagged `#curated` containing image attachments,
memos-ng automatically emails each photo — as a frame-optimized JPEG — to the
configured Aura frame address from the configured sender identity, exactly once per
attachment, with configuration living in the existing settings page and failures
visible and retried. Done means a `#curated` photo appears on the physical frame with
no manual step.

## Approach

**Trigger (router level, after linking, inside the existing transaction).** A
`maybe_enqueue_frame_shares(db, memo, user)` service helper is called from **both
router paths after all attachment linking is complete and before commit** —
`POST /memos` (after `svc.create_memo` returns) and `PATCH /memos/{uid}` (after
`link_attachments`, `app/routers/memos.py:269-275`). It must NOT
live inside `update_memo`: on the patch path, `svc.update_memo` runs **before**
`finalize_pending` + `link_attachments` (`app/routers/memos.py:269-274`), so an
in-service hook would miss attachments added in the same edit. The helper checks:
server SMTP config is populated (`SMTP_USERNAME`/`SMTP_PASSWORD` non-empty — the
global disable switch) AND the memo's tag set contains `curated` AND the owner has
both Aura settings populated. For each linked **image** attachment (any
Pillow-decodable image MIME; videos and other MIMEs are never enqueued), insert a
`frame_share_jobs` row with `ON CONFLICT (attachment_id) DO NOTHING`. The unique constraint on `attachment_id` is
the idempotency guard: an attachment is emailed **at most once ever**, so re-editing a
curated memo (which re-runs tag parsing) cannot resend, while newly added attachments
on an already-curated memo do get sent. Enqueue is atomic with the memo write (same
commit), mirroring how media jobs are enqueued at upload (`app/services/uploads.py:257`).

**Queue (new table, existing worker).** New `frame_share_jobs` table:
`id, attachment_id (FK CASCADE, UNIQUE), memo_id (FK CASCADE), user_id (FK CASCADE),
state (queued|claimed|sent|failed|skipped), attempts, next_attempt_at, error,
created_at, sent_at, claimed_by`. Cascades cover deletion races partially — but
`attachments.memo_id` is `ON DELETE SET NULL` (`app/models.py:127-128`), so a job
can survive its memo. Therefore the worker **revalidates at send time**: memo still
exists, is still tagged `curated`, the attachment is still linked to it, and the
owner's Aura settings are still populated — any failure marks the job `skipped`
(terminal, no email), as does a missing original file.
Deliberately **not** `media_jobs`: that table's `lane` enum encodes thermal routing
semantics and `process_job` hard-branches on lane+MIME; a third meaning would entangle
email sending with transcode throttling. The existing worker loop
(`app/worker.py:177-192`) gains a second claim step — same
`FOR UPDATE SKIP LOCKED` pattern — processing frame-share jobs opportunistically
between media jobs. Failure handling: increment `attempts`, exponential backoff
(re-queue with a `next_attempt_at` check or equivalent), terminal `failed` after 5
attempts with the SMTP error recorded; structlog on every transition. Send path
follows the push-notification shape: best-effort, own session, after commit, no-op
when unconfigured.

**Render (at send time, from the original).** A `render_for_frame(original_path) ->
bytes` pure-ish function using the existing Pillow + pillow-heif stack:
`exif_transpose`, convert to RGB, downscale to **longest edge 2400 px** (never
upscale), save JPEG **quality 88**, **preserving EXIF** with orientation reset to 1 —
critically keeping `DateTimeOriginal` so Aura's chronological ordering works. 2400 px
is ≥1.5× the largest current panel dimension (1600) and ≥1.17× the largest panel ever
(2048), so Aura's server-side resize never upscales, while typical output (1–3 MB)
stays far under the 9.5 MB proxy cap and iCloud's 20 MB message limit. HEIC originals
decode via pillow-heif and ship as JPEG (Aura's email path accepts JPG/PNG/TIFF only).
Rendering runs in the worker via `to_thread.run_sync`. The render is attached to the
email and discarded — no new stored tier. The original resolves from
`attachment.storage_path` — the same source `_tier_path` uses for the original tier
(`app/routers/assets.py:43-45`) — so the per-memo-folder layout change (plan 1667)
is transparent; no path construction from layout conventions.

**Email (aiosmtplib, config-driven).** New dependency `aiosmtplib`. One email per job
(Aura mandates one photo per email): empty subject/body (ignored by Aura), single
JPEG attachment named from the original filename. Server config via new `Settings`
fields `SMTP_HOST` (default `smtp.mail.me.com`), `SMTP_PORT` (587, STARTTLS),
`SMTP_USERNAME`, `SMTP_PASSWORD` — empty username/password disables the feature
globally (VAPID pattern). Per-user config via two new nullable `User` columns:
`frame_send_to` (the `…@send.auraframes.com` address) and `frame_send_from` (must be
an identity the SMTP account owns AND the email linked to an Aura member of the
frame — Phase 0 pins down which concrete address that is).

**Settings surface.** Extend `SettingsOut`/`SettingsPatch` and the PATCH handler
(`app/routers/settings.py`), regenerate OpenAPI types, and add an "Aura frame" card to
`SettingsView.vue`: send-to field, send-from field, and a **"Send test photo"** button
hitting a new `POST /api/settings/frame-test` that enqueues (or directly sends) a
test image through the full render+SMTP path and reports the SMTP outcome — labeled
in the UI as "accepted by mail server — check the frame", because relay acceptance is
not delivery. Email-address validation lives in a pure function unit-tested directly.

**Verification-first.** Phase 0 empirically resolves the two facts research could not:
whether the HME-style `honey.linens-45@icloud.com` is usable as From (expected: no),
and which sender address Aura accepts for this frame. Nothing is built until the
send path is proven with a 30-line probe script, because every downstream design is
parameterized by "which From works".

## Rejected Alternatives

- **Reuse the `media_jobs` queue with a new job type.** Its `lane` enum is a 2-value
  thermal-routing discriminator and `process_job` branches on lane+MIME with no type
  column (`app/services/media.py:245-279`); adding a third semantic would couple email
  retries/backoff to transcode throttling and complicate the thermal invariants. A
  dedicated table is ~40 lines and keeps both state machines honest.
- **Reuse the existing 2048 px mobile tier as the sent file.** Zero render cost, but
  it is quality 80 with EXIF stripped — losing `DateTimeOriginal` breaks Aura's
  chronological ordering, and q80 is below the "don't make it too small / send 100%
  of something" requirement. A dedicated at-send render costs a second of CPU per
  photo, a few times a day.
- **Reverse-engineer the Aura app API for direct upload.** Undocumented, unauthorized
  automation surface, breaks silently; email-to-frame is Aura's sanctioned ingestion
  path with documented sender authorization.
- **Global/server-level settings table for the frame config.** Per-user `User`
  columns already exist with a working API+UI pipeline, and Aura's sender rule (From
  = the member's own Aura account email) is inherently per-user.
- **Transactional relay (Postmark/SES) instead of iCloud SMTP.** Cannot legitimately
  send as `@icloud.com` (SPF/DKIM/DMARC misalignment) and Aura matches the sender to
  a member's address — the mail must genuinely come from the member's mailbox
  identity.
- **Frontend-triggered send (browser calls a send endpoint after upload).** Loses
  durability (tab closes, retries), and the trigger already lives naturally in the
  one service-layer choke point both create and edit paths share.

## Phased Breakdown

### Phase 0 — Empirical probe: iCloud From rules + Aura sender authorization
- **Branch:** none (no repo code changes beyond a throwaway script under `scripts/`,
  committed only if kept; probe runs from Merkur).
- **Build:** a minimal `scripts/frame_smtp_probe.py` (stdlib `smtplib`, argv: from,
  to, image path) that sends one small JPEG via `smtp.mail.me.com:587` STARTTLS with
  an app-specific password read from an env var (never a flag, never printed).
  **User inputs required (blocking):** (1) an app-specific password from
  account.apple.com (needs 2FA on the Apple ID); (2) the primary Apple ID address
  to authenticate as. Frame membership for `fabianmaass@me.com` is already done
  (2026-07-03). Probe matrix: (a) From = `fabianmaass@me.com` → frame address —
  the expected working pair; verify the photo appears on the frame (member check
  passes) rather than a rejection notification arriving; (b) optional curiosity:
  From = `honey.linens-45@icloud.com` → expect 55x rejection if it is Hide My
  Email; (c) if (a) fails at SMTP, retry with From = the primary Apple ID /
  `@icloud.com` variant and adjust the Aura membership to match.
- **Do NOT:** build any app code, add dependencies, or store the ASP anywhere but
  the local env for the probe run.
- **Verification (objective):** a probe photo is **visible on the physical Aura
  frame**, and the working (from, to) pair plus observed SMTP behavior for the
  failing cases are recorded as dated findings appended to this plan. This gate
  parameterizes everything downstream; if no From works, the plan stops here and
  reports.
- **Status:** pending

### Phase 1 — Backend: config, schema, enqueue-on-#curated
- **Branch:** `feature/aura-frame-email` (squash-merge to main; phases 1–3 may land
  as one PR or stacked PRs, each phase gate passing before the next starts).
- **Build:** `Settings` fields `SMTP_HOST/SMTP_PORT/SMTP_USERNAME/SMTP_PASSWORD` in
  `app/config.py` (empty = disabled; excluded from any logging/repr). Alembic
  migration: `users.frame_send_to`, `users.frame_send_from` (nullable strings) + new
  `frame_share_jobs` table (columns per Approach; UNIQUE on `attachment_id`; enum
  `FrameShareState {queued, claimed, sent, failed, skipped}`; `next_attempt_at` for
  backoff; FKs `ON DELETE CASCADE`). New service module
  `app/services/frame_share.py` with `maybe_enqueue_frame_shares(db, memo, user)`
  (guards: server SMTP config set, `curated` tag present, owner settings set) and
  the image-MIME predicate; **wired into the router paths `POST /memos` and
  `PATCH /memos/{uid}` after `link_attachments` and before commit**
  (`app/routers/memos.py:269-275`) — not inside `update_memo`, which runs before
  new uploads are linked. `uv run alembic check` clean.
- **Do NOT:** touch the worker, send any email, add `aiosmtplib`, or change any
  existing API response shape.
- **Verification:** pytest cases covering **every input category**: curated memo +
  image attachments (jobs created, one per image), curated + video-only (no jobs),
  un-tagged memo (no jobs), tag added on edit (jobs created), curated memo re-edited
  (no duplicate jobs — conflict path), new attachment added to already-curated memo
  (job for the new one only), owner missing send-to/from (no jobs), server SMTP
  config empty (no jobs even with user settings set). Local gate green:
  `uv run pyright app/ && uv run ruff check app/ tests/ scripts/ && uv run pytest -q
  && uv run alembic check`.
- **Status:** pending

### Phase 2 — Worker: render + SMTP send, retries, idempotent completion
- **Branch:** `feature/aura-frame-email` (continued).
- **Build:** add `aiosmtplib` to `pyproject.toml` (tight pin). `render_for_frame`
  in `app/services/frame_share.py`: Pillow open (pillow-heif already registered),
  `exif_transpose`, RGB, ≤2400 px longest edge (no upscale), JPEG q88, EXIF carried
  over with orientation normalized — run under `to_thread.run_sync`.
  `send_frame_share(job)` builds the one-attachment message and sends via aiosmtplib
  STARTTLS. Worker loop claims `frame_share_jobs` with the same
  `FOR UPDATE SKIP LOCKED` shape (honoring `next_attempt_at`), processes between
  media jobs; **send-time revalidation** (memo exists, still tagged `curated`,
  attachment still linked, owner settings still set, original file present) →
  terminal `skipped` on any miss, no email; attempts/backoff via `next_attempt_at`,
  terminal `failed` at 5 with error text; structlog transitions.
- **Do NOT:** persist the render to disk as a tier; send more than one photo per
  email; log credentials or full message bodies; touch `media_jobs` semantics.
- **Verification:** pytest with a **local fake SMTP dependency** (mock the SMTP
  transport, never the unit under test): success marks `sent` + `sent_at`; transient
  SMTP failure retries with backoff and preserves the job; 5th failure marks
  `failed` with the error recorded; revalidation cases (memo deleted → job removed
  by cascade; `#curated` removed before send → `skipped`, no SMTP call; attachment
  unlinked → `skipped`); render test asserts output ≤2400 px longest
  edge, JPEG, **EXIF DateTimeOriginal preserved**, orientation correct for a rotated
  fixture, HEIC fixture converts to JPEG. Removing the send call turns tests red
  (realness check). Because `pyproject.toml` changed: **local slim-image
  `docker build` must succeed before push.** Full local gate green.
- **Status:** pending

### Phase 3 — Settings API + UI + test-send
- **Branch:** `feature/aura-frame-email` (continued).
- **Build:** extend `SettingsOut`/`SettingsPatch` + PATCH handler with
  `frame_send_to`/`frame_send_from` (validated by a pure `is_valid_email`-style
  function); new `POST /api/settings/frame-test` sending a bundled test image
  through the real render+send path synchronously, returning the SMTP outcome.
  Regenerate `openapi.json` → `frontend/src/api/schema.d.ts` (api-drift CI job).
  `SettingsView.vue`: "Aura frame" card with the two fields and a "Send test photo"
  button showing "accepted by the mail server — check your frame" vs the error;
  extend `useSettings.ts`.
- **Do NOT:** add job-queue browsing UI; add per-frame multi-destination lists;
  gate the settings section behind feature flags.
- **Verification:** pytest for the settings round-trip (PATCH persists, GET returns,
  invalid email 422) and the test-send endpoint (fake SMTP: success and failure
  payloads); vitest for the extracted validation + any response-mapping logic;
  OpenAPI regenerated with no drift; full local gates (backend + frontend) green.
- **Status:** pending

### Phase 4 — Deploy + real-surface acceptance
- **Branch:** merge `feature/aura-frame-email` to main after the final cross-model
  audit; deploy via **/safe-deploy** (CI-built image, digest verify, `up -d`, never
  `restart`).
- **Build:** stack `.env` on Saturn gains `SMTP_USERNAME`/`SMTP_PASSWORD` (+ host/
  port if non-default); compose untouched otherwise (worker runs the same image).
  Configure send-to/send-from in the live settings page with the Phase-0-proven pair.
- **Do NOT:** deploy with a red or pending CI; skip the digest check; treat SMTP 250
  or a green log line as delivery.
- **Verification (named acceptance probe):** on production, upload a real photo in
  a memo tagged `#curated` → the photo **appears on the physical Aura frame** within
  the expected delivery window, correctly oriented, sorted by its capture date in
  chronological mode. Read-back evidence: `frame_share_jobs` row `sent`, worker log
  line, and a photo-on-frame confirmation (user eyeball or Aura app screenshot).
  Negative probe: edit the same memo — no second email (row count unchanged).
- **Status:** pending

## Execution Protocol (Agentic Review Loop)

Per phase, three gates in order:

1. **Pre-execution gate (before any code):** Codex reviews the phase's
   Build/Do-NOT/Verification spec against the current branch state. Invocation:
   write the prompt to a file, then `codex exec --sandbox read-only - < prompt.txt`
   (background, stdin closed/redirected). The prompt demands independent analysis
   FIRST, then the spec, then a typed verdict (`VERDICT: APPROVE | REVISE` +
   file:line evidence) in a fenced block appearing exactly once at the end; parse
   the LAST occurrence. Fold REVISE points in before implementing; delta rounds via
   `codex exec --sandbox read-only resume <session-id> - < delta.txt` (global flags
   before the subcommand).
2. **Implementation:** on `feature/aura-frame-email`, honoring the phase's Do-NOT
   cuts; the phase's verification commands must pass locally before requesting
   review. If reality contradicts the phase spec mid-implementation, stop — state
   what changed, update the plan, and re-run this phase's pre-execution gate if the
   change is material.
3. **Post-phase code gate:** Codex reviews the actual diff read-only. Preferred:
   `~/.claude/bin/agent-review-orchestrator.py` + `review-gate.py` (spec:
   `~/.claude/docs/agent-review-gate.md`); gate commands run by the orchestrator,
   real exit codes recorded. Fallback (self-contained): commit, then
   `codex exec --sandbox read-only` with a prompt to run `git diff main...HEAD`,
   read anything it doubts, and return the typed verdict; the implementer runs the
   verification commands and pastes real output into the prompt. Blocking verdicts
   must cite file:line or a reproducing command.

Before merge: a **final cross-model audit** of the full branch against the
Acceptance Criteria, item by item, with evidence — plus the standard release gates
(CI green all jobs, untracked-files check, slim-image build already proven in
Phase 2). If Codex is unavailable, substitute a fresh Claude subagent in an
isolated worktree with the same adversarial prompt — never skip a gate. Before
Phase 1 implementation, invoke `/disciplined-implementation` and carry its
disciplines through every phase. Close the plan with `/session-review` +
`/implementation-report`, then `/converge`.

## Dependencies

- **User-provided, blocking Phase 0:** an Apple app-specific password and the
  primary Apple ID address to authenticate as. Frame membership is already in
  place: `fabianmaass@me.com` was added as an Aura frame member on 2026-07-03 and
  is the planned From address.
- No other plan must land first. Plans 1667 (per-memo originals layout) and 1665
  (screen/print renditions) proceed independently; this plan reads originals via
  `attachment.storage_path`, so folder-layout changes are transparent.

## Dependencies on Current State

- `app/services/memos.py` — `parse_tags` (:28-35), `create_memo` (:70-96),
  `update_memo` (:264-277), `link_attachments` (:130-167).
- `app/worker.py` — claim loop (:63-81), `run_once` (:128-174), `run` (:177-192),
  push-notify pattern (:112-125).
- `app/services/media.py` — Pillow/pillow-heif stack (:17, :40-65), `_atomic_save`
  (:27-31); `app/routers/assets.py:43-45` original path via `attachment.storage_path`
  (`_derived_tier_path` at :29-40 covers derived tiers only).
- `app/config.py` — `Settings` (:13-119), VAPID empty-default pattern (:75-77),
  `validate_runtime_secrets` (:109-119).
- `app/routers/settings.py` (:16-58), `frontend/src/views/SettingsView.vue`,
  `frontend/src/composables/useSettings.ts`, OpenAPI → `schema.d.ts` generation
  (api-drift CI job).
- Saturn stack `.env` at `/volume1/docker-compose/stacks/` (SMTP secrets at deploy).
- Assumes production DB reachable by worker and `data/originals/...` mounted in the
  worker container (already true for media jobs).

## Risks

- **Sender identity mismatch.** `honey.linens-45@icloud.com` is Hide My Email
  format and expected to be unusable as SMTP From; largely defused by the user
  adding `fabianmaass@me.com` (an owned iCloud identity) as a frame member on
  2026-07-03. Residual risk: `@me.com` From over `smtp.mail.me.com` failing for
  this specific account — Phase 0 proves the pair in minutes, with the primary
  Apple ID / `@icloud.com` variant as fallback (plus matching Aura membership).
  The plan is parameterized by Phase 0's outcome, not by the assumed address.
- **App-specific password mass-revocation** whenever the Apple ID password changes —
  silent daemon breakage. Mitigation: failures land in `frame_share_jobs.failed`
  with the SMTP auth error recorded and structlog visibility; runbook note in the
  session review. Accepted residual risk for v1 (no push alerting).
- **Aura's email attachment cap is undocumented** (9.5 MB web-uploader proxy).
  Mitigation: 2400 px q88 JPEG is typically 1–3 MB; render asserts nothing exceeds
  ~9 MB (log + still send; empirical Phase-0/4 evidence governs).
- **SMTP 250 ≠ delivery** (LSN-0004/0008). Mitigation: acceptance probe is the
  photo on the physical frame; the UI test-send explicitly says "check your frame".
- **EXIF preservation is fiddly in Pillow** (orientation vs. carried-over tags).
  Mitigation: dedicated unit tests with a rotated fixture and a DateTimeOriginal
  assertion; Aura sorts by that tag in chronological mode.
- **`pyproject.toml` change breaks the slim image** (no compiler for C wheels).
  Mitigation: aiosmtplib is pure-Python; local slim `docker build` is a Phase 2
  gate regardless.
- **Thermal coupling on Saturn.** A handful of single-photo Pillow renders per day
  in the worker (already the home of heavy-image decodes) is negligible; no new
  transcode class is introduced.

## Acceptance Criteria

1. Creating a memo tagged `#curated` with N image attachments (owner configured)
   produces exactly N `frame_share_jobs` rows and, after worker processing, N emails
   — each a single JPEG ≤2400 px longest edge, quality ~88, EXIF DateTimeOriginal
   intact — sent from the configured From to the configured frame address.
2. A probe photo sent through the full production path is **visible on the physical
   Aura frame**, correctly oriented (Phase 4 named acceptance probe).
3. Editing an already-curated memo never resends existing attachments; adding a new
   image attachment to it sends only the new one. (Automated tests + Phase 4
   negative probe.)
3a. A queued job whose memo is deleted, un-tagged, or whose attachment is unlinked
   before the worker runs results in no email (cascade or terminal `skipped`).
4. Video and non-image attachments are never enqueued or emailed.
5. With server SMTP config or the user's send-to/from unset, no jobs are created,
   no errors surface, and all other behavior is unchanged.
6. The settings page persists send-to/send-from (validated), and "Send test photo"
   reports SMTP acceptance vs. error with wording that does not claim frame
   delivery.
7. Transient SMTP failures retry with backoff; the 5th failure marks the job
   `failed` with the error recorded and logged.
8. CI is green on all jobs (backend, frontend, api-drift, release) on the merge
   commit; the slim image built locally during Phase 2; no untracked source files
   at push time.

## Phase 0 Findings (2026-07-03) — PROVEN, gate passed

**Working send path (proven end-to-end):**
- AUTH as `fabianmaass@me.com` + app-specific password over `smtp.mail.me.com:587`
  STARTTLS. From = `fabianmaass@me.com`, To = `43cec2f1099546c6fad1@send.auraframes.com`.
- iCloud accepted every send (250 `queued as …`, zero per-recipient failures; e.g.
  queue id `ED25B1800133`).
- **Aura confirmed delivery on the real surface:** Aura emailed the sender
  *"We got your email and added your photo to Fabian's Frame!"* — the photo was added
  to the frame. This is the acceptance gate (not the SMTP 250). Note: Aura's email
  ingestion has minutes of latency — a photo not appearing instantly is normal.
- The HME-format `honey.linens-45@icloud.com` From (probe matrix case b) was NOT
  exercised — the `@me.com` member path worked first try, so the HME curiosity probe
  is unneeded.

**F1 — stored curated attachments are the "- screen.png" renditions and carry NO EXIF
`DateTimeOriginal`.** The curated memos' `attachments.storage_path` points at large
screen-rendition PNGs (e.g. 4047×2698, 17 MB), not the camera original, and these PNGs
have no `DateTimeOriginal`. Consequence for Phase 2: `render_for_frame` cannot rely on
source EXIF for Aura's chronological sort. **The DB has `attachments.captured_at`** —
Phase 2 should inject `captured_at` as the render's EXIF `DateTimeOriginal` when the
source lacks it, so frame ordering stays correct. (Revises the plan's assumption that
preserving source EXIF suffices.)

**F2 — quality/size decision (revises the Phase 2 render spec).** User requirement,
stated 2026-07-03: send maximum-quality photos, not the plan's 2400 px q88. Measured on
the 4047×2698 curated source, full-resolution, no chroma subsampling (4:4:4):
- q100 → **12.78 MB** — exceeds Aura's ~9.5 MB web-uploader proxy cap; likely rejected.
- q95 → **4.16 MB** — safe under the cap, visually indistinguishable from q100.
- (plan's 2400 px q88 → 654 KB — delivered fine but user-rejected as too low quality.)
**SUPERSEDED 2026-07-04 — full-res is UNDELIVERABLE, capped at 2048px.** The production backfill
proved the full-res decision below was wrong: real curated exports average ~8600px / 82MB source
(max 138MB), and full-res q95 rendered to 20-46MB JPEGs which iCloud rejects (`552 5.3.4 Message
size exceeds fixed limit`, 20MB cap) and blow Aura's ~9.5MB proxy cap. First 11 backfill sends all
failed. Fix (commit c53699d): `render_for_frame` downscales to a **2048px longest edge** (never
upscales) — = Aura's largest-ever panel, which it resizes to on ingestion anyway, so visually
lossless on the frame; renders drop to ~0.7-2MB. Verified live: the 138MB source renders to
2048×1365 / 2.25MB. The full-res paragraph below is retained only as the record of the reversed
decision.

**Phase 4 deploy (2026-07-04):** merged PR #21 (feature) + #22 (2048px cap fix) to main via CI
(all jobs green, Trivy-scanned images). Deployed to Saturn by digest pin
(`@sha256:d4faeb1b…`) + `up -d --force-recreate`; migration `f156a444d37e` applied on start.
SMTP creds in the Saturn stack `.env` (worker only — the Merkur slow-lane worker runs an old
0.1.0 image without frame code + no SMTP, so it never claims frame jobs; the `server_smtp_configured`
guard is belt-and-suspenders). Acceptance: a test image and the paced backfill photos land on the
physical frame (user-confirmed). **Backfill:** 166 pre-existing #curated screen images (print
renditions correctly excluded) enqueued and drained at ~45s spacing (user choice — keeps the
thermally-bound NAS cool; 0 thermal defers observed) over ~2h.

**DECIDED Phase 2 render spec (user-confirmed 2026-07-03; both a 2400 px q88 and a
full-res q95 4:4:4 render were frame-confirmed this session):** full resolution from
`storage_path` (no downscale; Aura resizes to its ≤2048×1536 panel server-side anyway),
**JPEG quality 95, subsampling 0 (4:4:4)**, EXIF preserved + `DateTimeOriginal` backfilled
from `captured_at`, orientation normalized to 1. Assert output < ~9 MB (log + still send if
exceeded, per the plan's Risk note). This supersedes "2400 px, quality 88" throughout the
plan. **Must go through the Phase 2 pre-execution Codex gate before implementation**
(material change to a Codex-approved plan).

**Deferred:** rotate the app-specific password — it was briefly exposed in this session
(SMTP debug transcript printed the AUTH PLAIN handshake; the leaked tool-result file was
deleted). Revoke + regenerate at account.apple.com, update `~/.config/memos-ng/frame.env`.
Env file is now owned by `fabian` (mode 600). **DONE 2026-07-03:** the user rotated the ASP
and updated `frame.env` with the new value.

## Phase 2 design decisions (pre-exec gate round, 2026-07-03) — resolves 2 REVISE blockers

The Phase 2 pre-execution Codex gate returned REVISE on two design points. Resolutions,
now authoritative for Phase 2 implementation:

**D1 — Delivery semantics = AT-MOST-ONCE (no duplicate on the frame).** A duplicate photo on
the frame is worse UX than a rare miss (which the user can re-trigger by re-adding). Mechanism:
the worker commits `state=claimed` (queued → claimed) BEFORE rendering/sending. The claim query
selects only `state=queued`, so a worker crash between "SMTP accepted DATA" and the `sent` commit
leaves the job stranded in `claimed` — it is NEVER re-claimed, so it cannot resend (the `claimed`
state is the in-flight tombstone). No stale-`claimed` reaper (a reaper would reintroduce the
duplicate risk). SMTP error classification (Codex note): permanent failures
(`SMTPAuthenticationError`, `SMTPSenderRefused`, `SMTPRecipientsRefused`, 5xx policy) →
terminal `failed` immediately with the error recorded (do NOT churn 5 attempts). Definitely-
no-send transient failures (`SMTPConnectError`, `SMTPServerDisconnected`, timeout — raised
before DATA acceptance) → `queued` with exponential `next_attempt_at` backoff, terminal
`failed` at 5 attempts. Residual accepted risk: the narrow "server accepted DATA then the
connection dropped so `aiosmtplib.send` raised" window could, in principle, retry-and-duplicate;
we treat a generic post-connection `SMTPException` as transient-retryable only if no crash
occurred, and accept the rare duplicate (deletable from the Aura app) rather than silently drop.

**D2 — Send-time revalidation is a BEST-EFFORT filter, NOT a lock-held guarantee.** We do NOT
hold a DB row lock on the attachment/`memo_tags` across the SMTP round-trip — that would block
memo edits for the seconds a send takes and pin a DB connection. Instead the worker re-reads
(memo exists, still tagged `curated`, attachment still linked with `memo_id == job.memo_id`,
owner settings still set, original file present) immediately before render/send inside the claim
transaction; any miss → terminal `state=skipped`, no email. This catches every realistic case
(memo deleted / un-tagged / attachment unlinked before the worker polls — and Aura ingestion is
minutes-latent, so edits almost always precede the send). Explicitly out of guarantee: a memo
un-curated in the final milliseconds AFTER revalidation but BEFORE the SMTP send completes may
still deliver one photo; consequence is minor (delete it from the Aura app). This is the
documented weakening the gate asked for, chosen over cross-send row locks.

**D3 — render_for_frame signature change (from F1):** `render_for_frame(original_path,
captured_at=None) -> bytes`. Inject `captured_at` as EXIF `DateTimeOriginal` in EXIF timestamp
format (`"%Y:%m:%d %H:%M:%S"`) only when the source lacks it; write EXIF with `Orientation`
reset to 1 AFTER `ImageOps.exif_transpose` normalizes pixels (no double-rotation). Log the
output byte size; still send if > ~9 MB (product choice, not correctness).
