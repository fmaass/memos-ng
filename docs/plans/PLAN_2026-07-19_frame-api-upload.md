---
name: "Switch #curated frame delivery to aura-frames album API"
overview: "Replace memos-ng's direct SMTP email delivery to Aura frames with an HTTP upload to the aura-frames project's album API. aura-frames already owns the frame delivery pipeline (queue, scheduler, SMTP) — memos-ng just needs to drop photos into the right album. Requires adding machine-token auth to aura-frames first."
scope: standard
project: "memos-ng"
feature: "frame-api-upload"
status: "shipped (app/services/frame_share.py; #31, #32)"
created: "2026-07-19"
todos:
  - id: "p1-upload-token"
    content: "Phase 1 — Upload-scoped bearer secret on aura-frames album upload"
    status: pending
  - id: "p2-upload-client"
    content: "Phase 2 — Replace memos-ng SMTP send with httpx upload (200/201, retry split)"
    status: pending
  - id: "p3-cleanup"
    content: "Phase 3 — Remove SMTP+UI, regen OpenAPI client, keep DB cols for rollback"
    status: pending
isProject: false
---

# Implementation Plan: Switch #curated frame delivery to aura-frames album API

## Context for Independent Review

**Current state (observed 2026-07-19):** memos-ng has a fully implemented #curated → Aura frame pipeline (plan `PLAN_2026-07-03_aura-frame-email`, status: all phases done). When a memo is tagged `#curated`, `maybe_enqueue_frame_shares` inserts rows into `frame_share_jobs`. The worker drains the queue: renders each image to a 2048px JPEG (ADR-0012), then emails it via aiosmtplib through iCloud SMTP (`smtp.mail.me.com:587`) to the user's configured `@send.auraframes.com` address. Per-user settings (`frame_send_to`, `frame_send_from` on the `users` table) are exposed in the frontend SettingsView with a "Send test photo" button.

Separately, the `aura-frames` project (`/home/fabian/projects/aura-frames`, deployed at `frames.discomarder.live` on Jupiter) provides a dedicated photo album → frame management system. It accepts photo uploads (`POST /api/albums/{id}/photos`, raw binary + `X-Filename` header), auto-syncs uploads to following frames via `album_sync.trigger_album_fanout()`, and handles the SMTP delivery to Aura via its own scheduler. A target album "Damian #curated" already exists in production (the "Fabian & Laura" frame follows "Esperando a Damian" album, observed 2026-07-19).

**Problem / motivation:** Two systems independently implement Aura SMTP delivery — memos-ng's `frame_share.py` and aura-frames' scheduler. This is duplicated infrastructure. aura-frames is the purpose-built system for album/frame management and already handles queuing, retry, dedup, and delivery scheduling. memos-ng should delegate delivery to it by uploading to the album API and letting aura-frames handle the rest.

**Scope boundary:**
- IN: an upload-scoped bearer secret in aura-frames, httpx upload client in memos-ng, removal of memos-ng's SMTP frame code and per-user settings UI.
- OUT: changes to the aura-frames scheduler itself, changes to the album/frame follow model, multi-user support (this is a single-household system with one configured album), migration of historical `frame_share_jobs` data (rows stay for audit; the table can be dropped in a future cleanup).
- OUT (deferred enhancement, separate plan): reading Aura's confirmation/rejection emails back over IMAP to close the delivery-confirmation gap (Fastmail `250 accepted` proves relay, not frame arrival). Rationale for deferral: Aura's confirmation email is an undocumented, brittle-to-parse format with weak per-photo correlation; IMAP grants read+delete on a personal mailbox (privacy/blast-radius); and auto-deleting mail is destructive. The inbox-clutter goal is better solved now with a Fastmail server-side rule (no code). If built later, gate behind: a dedicated frame-only mailbox, move-to-folder (never hard-expunge), a tight sender filter, and a parser canary — and prioritize the rejection/bounce signal (which would have caught the 2026-07-19 `@me.com` de-authorization automatically) over success confirmations.

**Hard constraints:**
- The aura-frames album upload API contract is fixed: `POST /api/albums/{album_id}/photos` with raw binary body, `Content-Type: image/jpeg`, `X-Filename: <name>.jpg` header. Response: 201 on success.
- The 2048px render must stay in memos-ng (ADR-0012: Aura's largest panel is 2048x1536; originals are 8600px/82MB and must not transit the network at full size).
- At-most-once delivery semantics (ADR-0011) must be preserved: a claimed job must not re-upload on crash recovery.
- The aura-frames scheduler must be enabled (`SCHEDULER_ENABLED=true`) for end-to-end delivery to work. Currently disabled in production.
- aura-frames currently authenticates only via trusted-proxy `Remote-User` headers (Authelia). Machine clients need a bearer token path — the code already has a structural hook (CSRF exemption for `Authorization: Bearer` in `enforce_csrf`, and `Identity.source` field with a comment "bearer added in a later phase").

**Terminology & decisions already locked:**
- **ADR-0002 (aura-frames, page 2061, status: proposed):** "aura-frames is the sole owner of Aura frame delivery (memos feeds albums, stops emailing frames)." THIS PLAN is the implementation of that ADR. Building it is the trigger to promote ADR-0002 proposed → accepted.
- "Album API" = the aura-frames REST endpoint for photo upload
- ADR-0012 (memos-ng): 2048px longest-edge cap — do not re-litigate
- ADR-0011 (memos-ng): at-most-once delivery via committed claim tombstone — the `claimed` state semantics carry over (httpx upload replaces SMTP send, same idempotency boundary)
- The target album in aura-frames is pre-created. The album_id is a configuration value, not discovered at runtime.
- **Target album is `album#5 "Damian #curated"` (`FRAME_ALBUM_ID=5`)** — the dedicated album created for this feature (user-confirmed 2026-07-19). This is NOT the same as `album#4 "Esperando a Damian"`, which the frame already follows. Because album#5 has NO frame follower yet, a follow MUST be added (see Phase 2 prerequisite) or uploaded photos will sit in the album and never reach a frame.

**Dated production evidence (2026-07-19):**
- aura-frames DB: frame#2 "Fabian & Laura" enabled=True, `aura_email=b8c21a6d79c310d4bf5d@send.auraframes.com`
- aura-frames DB: frame "Yas & Flo" enabled=False
- aura-frames DB: albums — #4 "Esperando a Damian" (followed by frame#2), **#5 "Damian #curated" (the target — currently followed by NO frame)**
- aura-frames container env (AFTER this session's live work): `SCHEDULER_ENABLED=true`, SMTP now **Fastmail** (`smtp.fastmail.com`, `FRAME_SEND_FROM=fabianmaass@fastmail.fm`); the Aura frames were reconfigured to accept only the Fastmail sender (the old iCloud `@me.com` sender is de-authorized)
- memos-ng: `frame_share_jobs` table exists (201 sent, 0 queued); the per-user direct-send path was **disabled this session** (user#3 `frame_send_to`/`frame_send_from` cleared) to stop `@me.com` bounces — so `#curated` photos reach no frame until this plan is built

**Hard-won constraints from prior sessions:**
- aura-frames requires both `Origin` header AND the origin in `APP_ORIGINS` for browser CSRF — but bearer requests are CSRF-exempt (already implemented in `enforce_csrf`)
- aura-frames uses `X-Filename` header (not multipart form upload) for photo ingestion
- The `album_sync.trigger_album_fanout()` fires automatically on upload when album has followers — no manual sync trigger needed

### Decisions most likely to change

1. **Machine auth mechanism (aura-frames):** An env-based, **upload-scoped** shared secret (`FRAME_API_TOKEN`), constant-time compared with `hmac.compare_digest`, gating ONLY the album-upload endpoint — not a full-admin credential. Alternative considered and rejected: a DB-backed `api_tokens` table with hashed tokens (see Rejected Alternatives #5). The env-secret keeps `resolve_identity` synchronous (no async/DB refactor of the auth core), grants least privilege (upload only, never album/frame deletion), and needs no migration. Changing after build: swap to a DB table, medium cost.

2. **Album target configuration (memos-ng):** Single env var `FRAME_ALBUM_ID=5` (album#5 "Damian #curated", user-confirmed). Alternative: per-user album mapping in DB. Changing after build: one env var (or migration + settings UI if per-user), low/medium cost — per-user is unnecessary for a single-household system.

3. **SMTP removal scope:** Remove the SMTP *frame-delivery* path from memos-ng (frame_share.py send logic, aiosmtplib dep, settings UI section). The per-user `frame_send_to`/`frame_send_from` columns are made **nullable-and-unused** (kept, not dropped) for one rollback release — see Phase 3. Alternative: keep SMTP as fallback. Changing after removal: re-add from git history, low cost.

4. **HTTP success contract (memos-ng client):** BOTH `201` (new photo) and `200` (SHA-duplicate) are treated as success — the aura-frames upload endpoint returns 200 when the same image already exists in the album (`app/routers/albums.py:489`). Treating only 201 as success would record a successful-but-duplicate delegation as `failed` on a lost-response retry. Changing after build: one-line predicate + test, low cost.

## Goal

Replace memos-ng's direct SMTP delivery to Aura frames with an HTTP upload to the aura-frames album API, making aura-frames the single owner of the frame delivery pipeline. This eliminates duplicated SMTP infrastructure, simplifies memos-ng's codebase, and consolidates frame management in the purpose-built system.

## Approach

Three-phase approach ordered by dependency:

1. **aura-frames gets an upload-scoped bearer secret** — a `FRAME_API_TOKEN` env var and a new async FastAPI dependency `require_upload_auth` on the album-upload route. It accepts a request when `Authorization: Bearer <token>` matches the env secret (`hmac.compare_digest`), and otherwise falls back to the existing `require_admin` (so a browser admin can still upload). This deliberately does NOT touch the synchronous `resolve_identity` core (`app/auth.py:76`) — no async/DB refactor of the auth path, and the machine credential grants upload only, never full admin. CSRF is already bearer-exempt (`app/auth.py:148-150`).

2. **memos-ng's send path switches to httpx** — `process_claimed_job` replaces the SMTP send with an httpx POST to the album API (explicit 30s timeout). The render step stays identical (2048px JPEG). Configuration changes from the SMTP frame vars to `FRAME_API_URL`, `FRAME_ALBUM_ID`, `FRAME_API_TOKEN`. The worker gate changes from `server_smtp_configured()` to a new `frame_api_configured()`. Both HTTP 200 and 201 are success; the transient/permanent error split is redefined for HTTP status codes.

3. **Dead code removal** — strip the SMTP frame-send path and aiosmtplib dependency, remove the settings UI "Aura frame" section and the frame-test endpoint, remove the frame fields from the settings API schema, **regenerate the OpenAPI client**, and clean up the frontend `useSettings` composable. The `frame_send_to`/`frame_send_from` DB columns are kept (nullable, unused) for one rollback release.

## Rejected Alternatives

1. **Trusted-proxy hack (synthetic Remote-User header):** Would avoid a new auth path in aura-frames, but mixes authentication concerns — a machine client pretending to be a browser user by forging proxy headers is fragile and audit-invisible. The bearer token path is already structurally prepared in the code.

2. **Keep SMTP as fallback:** Adds maintenance burden for a path that should never activate once the API upload works. If aura-frames is down, memos-ng queuing with retry is sufficient — the job stays `queued` until the API responds 201.

3. **Move the render to aura-frames:** Would require uploading 82MB originals over the network. ADR-0012 decided the 2048px cap lives at the source. aura-frames doesn't resize at ingest and would need new render infrastructure.

4. **Per-user album mapping:** Unnecessary complexity for a single-household system. A single `FRAME_ALBUM_ID` env var is sufficient and can be extended later if needed.

5. **DB-backed `api_tokens` table with hashed tokens (the original Phase 1):** Rejected after cross-model review. It would require a migration, a token-CRUD surface, AND making `resolve_identity` (`app/auth.py:76`, currently synchronous with no DB session, called synchronously at `app/auth.py:117` and `app/routers/auth.py:27`) async with an injected session plus updating both callers — a disproportionate refactor of the security-critical auth core for a single machine client. The env-based `hmac.compare_digest` secret grants least privilege (upload-scoped) and touches none of that core. A DB table is the right shape only when there are multiple clients or per-token revocation/audit requirements — neither applies here.

## Phased Breakdown

### Phase 1 — Upload-scoped bearer secret in aura-frames

- **Branch:** `feature/upload-token` in aura-frames repo
- **Build:**
  - `app/config.py`: add `FRAME_API_TOKEN: str = ""` (the shared secret; empty ⇒ bearer upload disabled, browser-admin still works). No new secret is required at rest beyond this env var.
  - `app/auth.py`: add a new **async** dependency `require_upload_auth(request: Request) -> Identity`:
    - If `Authorization: Bearer <tok>` is present AND `settings.FRAME_API_TOKEN` is non-empty AND `hmac.compare_digest(tok, settings.FRAME_API_TOKEN)` → return `Identity(user="frame-api", is_admin=False, source="bearer")` (constant-time compare; never log the token).
    - A bearer header present but non-matching (or `FRAME_API_TOKEN` empty) → raise 401 (fail-closed; do NOT silently fall through to admin, which would let a wrong token probe the browser path).
    - No bearer header at all → delegate to `await require_admin(request)` (browser admin retains upload access).
    - This function is NEW and additive. It does NOT modify the synchronous `resolve_identity` (`app/auth.py:76`) or its callers (`app/auth.py:117`, `app/routers/auth.py:27`) — leaving the browser auth core untouched.
  - `app/routers/albums.py`: change ONLY the photo-upload endpoint's dependency from `require_admin` to `require_upload_auth`. All other album/frame routes keep `require_admin` (the machine token cannot delete albums or frames — least privilege).
  - CSRF: no change needed — `enforce_csrf` already exempts `Authorization: Bearer` requests (`app/auth.py:148-150`).
  - Tests: valid token → 201 on new upload; **valid token, duplicate image → 200** (the SHA-dup path, `uploads.py:855-862`); wrong/absent-with-empty-config token → 401; bearer request bypasses CSRF (no Origin header) → still 201/200; browser-admin (no bearer) upload still works; a NON-upload admin route with the bearer token → 401 (proves the token is upload-scoped, not full admin).
- **Do NOT:** Add a DB `api_tokens` table, a token-CRUD router, OAuth2/JWT, per-token audit, rate limiting, or any change to `resolve_identity`. Do not grant the bearer identity `is_admin=True`.
- **Verification:**
  ```
  cd /home/fabian/projects/aura-frames && uv run pytest -q
  # Manual: upload a photo with the bearer secret
  curl -i -X POST http://localhost:8091/api/albums/<ALBUM_ID>/photos \
    -H "Authorization: Bearer $FRAME_API_TOKEN" \
    -H "Content-Type: image/jpeg" -H "X-Filename: test.jpg" \
    --data-binary @test.jpg
  # First upload → 201; identical re-upload → 200 (both success)
  # A delete with the same token must be rejected:
  curl -s -o /dev/null -w '%{http_code}' -X DELETE \
    -H "Authorization: Bearer $FRAME_API_TOKEN" \
    http://localhost:8091/api/albums/<ALBUM_ID>   # expect 401
  ```
- **Status:** pending

### Phase 2 — Replace memos-ng SMTP send with album API upload

- **Branch:** `feature/frame-api-upload` in memos-ng repo
- **Prerequisite (aura-frames, one-time config — do FIRST):** make frame#2 "Fabian & Laura" follow `album#5 "Damian #curated"` (`POST /api/albums/5/frames/2` with `{"include_existing": false}`, or via the admin UI). Without this follow, `album_sync.trigger_album_fanout()` materializes nothing and uploaded photos never reach the frame. Verify: the follow row exists in `frame_album_follows` (album_id=5, frame_id=2).
- **Build:**
  - `app/config.py`: add `FRAME_API_URL: str = ""`, `FRAME_ALBUM_ID: int = 0` (set to `5` in prod .env), `FRAME_API_TOKEN: str = ""`. Keep existing SMTP_* settings (used for other features).
  - `app/services/frame_share.py`:
    - New function `frame_api_configured() -> bool` (all three settings non-empty/non-zero)
    - Replace `send_frame_share()` with `upload_to_album(filename: str, jpeg_bytes: bytes) -> None` using httpx: `POST {FRAME_API_URL}/api/albums/{FRAME_ALBUM_ID}/photos` with `Authorization: Bearer {FRAME_API_TOKEN}`, `Content-Type: image/jpeg`, `X-Filename: {filename}` headers, body = jpeg_bytes, **explicit `timeout=httpx.Timeout(30.0)`** (do not inherit httpx's default; aura-frames validates + generates a thumbnail synchronously on ingest, so give it the same headroom the old `SMTP_TIMEOUT_S=30` had). **Success = HTTP 200 OR 201** — the album endpoint returns 201 for a new photo and **200 for a SHA-duplicate** (`aura-frames app/routers/albums.py:489`); both mean the photo is safely in the album. Raise a typed error only on any other status / transport failure.
    - Update `process_claimed_job`: call `upload_to_album` instead of `send_frame_share`. Redefine the transient/permanent split for HTTP (replacing `_is_permanent_smtp_error`):
      - **Transient (retry with backoff):** connection errors, read/connect timeouts, HTTP 408, 429, and all 5xx.
      - **Permanent (fail terminally):** all other 4xx (400, 401, 403, 404, 413, 415, 422) — a mis-configured token/album or a rejected payload will not fix itself on retry.
      - A duplicate 200 is neither — it is success (job → `sent`), and critically this makes a lost-response retry idempotent: the re-upload returns 200 and the job completes rather than erroring.
    - Update `maybe_enqueue_frame_shares` guard: replace `server_smtp_configured()` with `frame_api_configured()`. Remove the `frame_settings_valid(user)` check (destination is now system-level, not per-user).
    - Update `_revalidate`: remove the per-user settings check; keep the memo-still-#curated / attachment-present / file-exists checks.
  - `app/worker.py`: the frame-share drain loop already exists; update its gate from `frame_share.server_smtp_configured()` to `frame_share.frame_api_configured()`.
  - `pyproject.toml`: ensure `httpx` is a runtime dependency (it is currently only a test/dev dep via the FastAPI test client — promote it to a main dependency). aiosmtplib stays until Phase 3.
  - Tests: replace SMTP mocks with a captured httpx transport (`httpx.MockTransport`). Cover: new upload → 201 → job `sent`; **duplicate → 200 → job `sent`** (idempotent retry); 500 → job stays `queued` with backoff; 422 → job `failed`; timeout → retry. Test the full enqueue→render→upload flow.
- **Do NOT:** Remove SMTP code yet (Phase 3). Change the enqueue trigger logic (#curated tag detection stays identical). Modify the render path.
- **Verification:**
  ```
  cd /home/fabian/projects/memos-ng
  uv run pyright app/ && uv run ruff check app/ tests/ && uv run pytest -q
  # Integration: configure FRAME_API_URL/ALBUM_ID/TOKEN in .env, tag a memo #curated,
  # then confirm the photo landed in the aura-frames album. The upload-scoped token
  # CANNOT read the album (GET /api/albums/{id}/photos is require_admin — by design,
  # least privilege), so verify via one of: (a) an aura-frames DB check
  # `SELECT id, filename, sha256 FROM album_photos WHERE album_id=<id> ORDER BY id DESC`,
  # or (b) the aura-frames admin UI (browser-admin session). Also confirm a second
  # identical enqueue returns 200 and does NOT mark the job failed.
  ```
- **Status:** pending

### Phase 3 — Remove dead SMTP frame code, settings UI, and regenerate the API client

- **Branch:** `feature/frame-cleanup` in memos-ng repo
- **Build (backend):**
  - `app/services/frame_share.py`: remove `send_frame_share()`, `SMTP_TIMEOUT_S`, `_is_permanent_smtp_error()`, `server_smtp_configured()`, `frame_settings_valid()`, `is_aura_send_to()`, `AURA_SEND_DOMAIN`, `_EMAIL_RE`, `is_valid_email()`, `render_test_image()`. Remove the `aiosmtplib` import.
  - `app/routers/settings.py`: remove `frame_send_to`/`frame_send_from` from `SettingsOut` and `SettingsPatch`, remove the `frame_test` endpoint and its cooldown map, remove the Aura frame field validators.
  - `app/models.py`: **keep** the `frame_send_to`/`frame_send_from` columns (nullable, unused) — do NOT drop them this release. This preserves a schema-compatible rollback to the prior app version. A follow-up cleanup ticket drops them once the API-upload path has soaked. (Rationale: dropping now makes rollback schema-incompatible — Codex advisory.)
  - `pyproject.toml`: remove `aiosmtplib` dependency.
- **Build (API contract — REQUIRED, gated by CI `api-drift`):**
  - Regenerate the OpenAPI schema and the typed client so `openapi.json` and `frontend/src/api/schema.d.ts` no longer reference the frame settings fields / `frame-test` path:
    `uv run python scripts/gen-openapi.py` then `cd frontend && npm run gen:api`. Commit both regenerated files. (`.github/workflows/ci.yml:60-77` fails the build on any drift between the live schema and the generated client — skipping this makes CI red even though local pytest is green.)
- **Build (frontend):**
  - `frontend/src/views/SettingsView.vue`: remove the "Aura frame" section entirely (send-to/send-from fields + test button).
  - `frontend/src/composables/useSettings.ts`: remove `frame_send_to`/`frame_send_from` from the `SettingsPatch` interface (lines ~5-15), remove the `FrameTestResult` interface, the `frameTest` mutation and its `/api/settings/frame-test` call (lines ~73-83), and the `frameTestMessage` pure helper; drop `frameTest` from the returned object.
  - `frontend/src/composables/__tests__/useSettings.spec.ts`: remove/adjust the frame-test cases so the suite matches the trimmed composable.
- **Tests:** remove/update all SMTP-mocking frame tests; assert the settings endpoint no longer exposes the frame fields; frontend suite green after the composable trim.
- **Do NOT:** Drop the `frame_share_jobs` table (historical audit trail) or the `frame_send_to`/`frame_send_from` columns (rollback safety this release). Remove the #curated enqueue logic (it stays, now on the upload path). Touch any non-frame SMTP usage if one exists.
- **Verification (must pass ALL — mirrors the CI jobs):**
  ```
  cd /home/fabian/projects/memos-ng
  uv run pyright app/ && uv run ruff check app/ tests/ && uv run pytest -q && uv run alembic check
  # API-drift gate (the one the plan previously omitted):
  uv run python scripts/gen-openapi.py && (cd frontend && npm run gen:api)
  git diff --exit-code -- openapi.json frontend/src/api/schema.d.ts   # must be clean after commit
  # Frontend gates:
  cd frontend && npm run lint && npm run typecheck && npm run test && npm run build
  # Greps:
  grep -rn "aiosmtplib" app/ pyproject.toml            # nothing
  grep -rn "frame_send_to\|frame-test\|frameTest" app/ frontend/src   # nothing (columns in models.py excepted)
  ```
- **Status:** pending

## Execution Protocol (Agentic Review Loop)

Per phase, three gates in order:

1. **Pre-execution gate (before any code):** Codex reviews the phase's Build/Do-NOT/Verification spec against the current branch state. Invocation: write the prompt to a file, then `codex exec --sandbox read-only -C <repo-path> - < prompt.txt` (background). The prompt demands independent analysis FIRST, then the spec, then a typed verdict (`VERDICT: APPROVE | REVISE` + file:line evidence) in a fenced block appearing exactly once at the end; parse the LAST occurrence. Fold REVISE points in before implementing; delta rounds via `codex exec resume <session-id>`.

2. **Implementation:** on the feature branch, honoring the phase's Do-NOT cuts; the phase's verification commands must pass locally before requesting review. If reality contradicts the phase spec mid-implementation, stop — update the plan and re-run this phase's pre-execution gate if the change is material.

3. **Post-phase code gate:** Codex reviews the actual diff read-only via `~/.claude/bin/agent-review-orchestrator.py`. Blocking verdicts must cite file:line or a reproducing command. Advisory findings ship as backlog.

Before merge: a **final cross-model audit** of the full branch against the Acceptance Criteria, item by item, with evidence.

## Dependencies

- Phase 1 (aura-frames bearer auth) must complete before Phase 2 can be tested end-to-end
- Phase 2 must be deployed and verified before Phase 3 removes the old path
- aura-frames scheduler must be enabled (`SCHEDULER_ENABLED=true`) for end-to-end delivery

## Dependencies on Current State

- aura-frames deployed and healthy at `frames.discomarder.live` (confirmed 2026-07-19); scheduler enabled, Fastmail SMTP verified
- Target album exists in aura-frames production: `album#5 "Damian #curated"` — but requires a NEW follow from frame#2 "Fabian & Laura" (Phase 2 prerequisite); it is currently unfollowed
- memos-ng deployed at `memos-ng.discomarder.live` with the existing SMTP frame pipeline operational
- Both repos cloned locally: `/home/fabian/projects/aura-frames`, `/home/fabian/projects/memos-ng`
- PostgreSQL databases accessible for both projects

## Risks

1. **aura-frames scheduler drains the GLOBAL queue when enabled.** Enabling `SCHEDULER_ENABLED=true` sends every eligible queued photo on every enabled frame, not just the new memos-ng upload (`aura-frames app/scheduler.py:197-214`). Mitigation: inventory and approve the full queue immediately before enabling (Codex advisory). NOTE: as of 2026-07-19 the scheduler is already enabled and verified in production (1 queued photo drained, Fastmail `250 accepted`), so this risk is largely retired — but re-check the queue before any future toggle.

2. **Network partition between Jupiter services.** If memos-ng can't reach aura-frames over the local network, uploads fail transiently. Mitigation: the transient/permanent split keeps connection failures + timeouts + 5xx in the retry-with-backoff bucket; both services run on the same machine (Jupiter) so partitions are extremely unlikely.

3. **Lost-response double-upload.** If aura-frames commits a photo but its HTTP response is lost, the job retries. Mitigation: aura-frames dedups on `(album_id, sha256)` and returns 200 for the duplicate; the client treats 200 as success, so the retry completes idempotently (no duplicate photo, no false `failed`).

4. **Token exposure.** The `FRAME_API_TOKEN` in memos-ng's `.env` on the same machine as aura-frames is a low-risk, upload-scoped credential (local network only, cannot delete albums/frames). Mitigation: constant-time compared, never logged; plaintext only in already-protected env vars. Rotating it is a two-env-var change with no migration.

## Acceptance Criteria

1. A memo tagged `#curated` with an image attachment in memos-ng results in that image appearing in the configured aura-frames album (verified via an aura-frames `album_photos` DB row or the admin UI — NOT the upload-scoped token, which cannot read) within one worker cycle.
2. The aura-frames scheduler sends the photo to the physical Aura frame via Fastmail SMTP — end-to-end from memo tag to frame delivery (`250 accepted` from an authorized sender).
3. The upload-scoped bearer secret authenticates upload requests without proxy headers or a CSRF Origin, AND is rejected on non-upload admin routes (least privilege) and on a wrong/empty token (401).
4. Both a new upload (201) and a duplicate re-upload (200) complete the job as `sent`; a simulated lost-response retry does not mark a delivered photo `failed`.
5. The transient/permanent error split retries on 5xx/408/429/timeout/connection-error and fails terminally on other 4xx (asserted by unit tests).
6. memos-ng no longer depends on aiosmtplib for frame delivery; the settings UI no longer shows the "Aura frame" section; `openapi.json` and `frontend/src/api/schema.d.ts` are regenerated and the CI `api-drift` gate is green.
7. At-most-once semantics are preserved: a crash between claim and upload does not double-upload on recovery (the committed-claim tombstone plus sha-dedup at aura-frames).
8. The `frame_share_jobs` table AND the nullable `frame_send_to`/`frame_send_from` columns are preserved this release (audit + schema-compatible rollback); a follow-up ticket drops the columns after soak.
