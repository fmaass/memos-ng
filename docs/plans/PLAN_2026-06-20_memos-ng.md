---
type: implementation-plan
project: memos-ng
feature: Memos-NG — Memos rebuilt on the default web stack
status: "partial (the app shipped; still open: T3.2 posting during an upload, and the tag autocomplete tracked as MEMNG-52)"
scope: major
created: 2026-06-20
last_updated: 2026-06-20
last_change: G1 closed (fixtures captured); G4/G5 added; T0.3 validate-config gate; T4.1 auth/file lines corrected
sessions: 0
durable_record: BookStack page 1649 (book 1648 "memos-ng — Features")
supersedes: BookStack page 1646 (portainer-stacks draft — re-tag at G3)
verified_against: BookStack 1646, 1647, 1429 (ADR-0002), 1504 (ADR-0004)
---

# Implementation Plan: Memos-NG

> Local working copy. The durable record is **BookStack page 1649** (book
> "memos-ng — Features", 1648). This plan folds a verification verdict and a
> pre-build-gate / critical-path layer on top of the original roadmap (page 1646,
> now superseded). 24 tasks, 6 phases, 0 implemented.

---

## Verification Verdict (the roadmap review the user asked for)

**Verdict: AGREE — the roadmap is sound, comprehensive, and ready to build, with two
pre-build gates that must close before the first line of T1.2 / T4.1 code.**

What I checked and what holds up:

- **Target is genuinely greenfield.** `~/projects/memos-ng/` is empty (no git, no
  files) — confirmed. Nothing to reconcile; the plan starts from a clean scaffold.
- **Referenced skeleton repos exist.** `~/projects/boat-ctl` and
  `~/projects/radioeins-collector` are present and are correctly characterised in
  the plan (boat-ctl = SPA skeleton; radioeins = proxy-auth, so only its
  `/api/auth/user` *response shape* is reusable, not its auth flow). `~/projects/portainer-stacks`
  (the stacks/satellites repo) is present.
- **The #1 open gate is now CLOSED.** The 2026-06-19 session review flagged "ADR-0002
  may not sanction a no-middleware app-managed OIDC boundary" as the highest-risk
  unknown. I read ADR-0002 (BookStack 1429, accepted): it explicitly says *"Use OIDC
  (via Authelia as the identity provider) for applications that have mobile clients
  or need direct API access without browser redirects."* memos-ng (iOS-PWA-primary +
  satellite API consumers) is exactly that class. The app-managed OIDC boundary is
  sanctioned. ADR-0002 does not separately discuss "no `chain-authelia@file` in front,"
  but that follows necessarily — an app running its own authorization-code flow cannot
  also sit behind forward-auth that intercepts the redirect. **Action:** ADR-0005
  records the confidential-BFF + no-middleware specifics as a *refinement* of ADR-0002,
  not a new sanction. T1.2's "confirm ADR-0002" sub-task is downgraded from blocker to
  a documentation cross-reference.

Where the plan is strongest: the media pending-state design (the actual reason for the
rebuild), the source-verified compat contract (T4.1 — the field list was re-derived
from satellite source after the first pass silently dropped memo `content`), the
explicit thermal-layer story for transcoding, and the rejected-alternatives section
(Pinia, Celery/Redis, Nuxt, imgproxy fallback, fork-upstream — each with a real reason).

Two residual gates and one hygiene item remain (see **Pre-Build Gates** below): they are
about *verifying inputs on real infra*, not about plan soundness.

---

## Context for Independent Review

A reviewer with zero prior knowledge needs this to scrutinise the plan.

**What exists today.** Memos runs in production at `memos.discomarder.live` as
`neosmemo/memos:0.29.1` plus **three support containers**: `memos-nginx` (URL-rewrite
reverse proxy for derived media), `memos-imgproxy` and `memos-resizer` (image
derivatives), with a **fourth** out-of-stack piece — a `memos-transcoder` on Merkur that
polls every 5 minutes over NFS to produce H.264 mp4s. Data: **87 memos, 515 attachments,
2 users, ~23 GB** of files at `/volume1/docker/memos/` on Saturn, with derived tiers at
`assets/{thumbs,mobile,web}/{uid}.*`. Four satellite consumers read this system via the
memos v1 API and/or the disk layout: **daily-dami-memos-mail** (Cronicle 07:00),
**memos-slideshow** (Merkur), **memos-transcoder** (Merkur), and rme/rausmitdir. These
counts and the live-API recon are **taken on the user's word — not yet verified against
the Saturn DB** (see Gate G2).

**What this plan builds.** A from-scratch replacement, `memos-ng`, owning the entire
codebase: Vue 3 + PrimeVue 4 + Tailwind v4 SPA, FastAPI + SQLAlchemy 2 async backend,
shared `postgres17` DB `memosng`, Authelia OIDC via a **confidential BFF** (tokens
server-side, httpOnly session cookie). Two runtime containers from one image:
`memos-ng-app` (FastAPI, serves the SPA + clean `/api/*` REST + a read-only `/api/v1/*`
+ `/file/*` compat router + SSE) and `memos-ng-worker` (same image, `command: worker` —
pyvips + ffmpeg media pipeline, DB-backed `media_jobs` queue with
`SELECT … FOR UPDATE SKIP LOCKED`). Built and proven at `memos-ng.discomarder.live`,
takes over `memos.discomarder.live` only at cutover (Phase 5). memos-ng is intended to
become the **new default-stack reference project**, superseding boat-ctl.

**Scope boundary — explicitly OUT.**
- No fork or patch of upstream Go memos (rejected — see Rejected Alternatives).
- No Celery/Redis/arq job runner — a Postgres `media_jobs` table is the queue.
- No Pinia — composables for app/UI state, **TanStack Query for server-state**.
- No Nuxt/SSR — a Vite SPA served by FastAPI.
- No on-the-fly imgproxy fallback — pre-generation is queue-guaranteed.
- No nginx tiering container — FastAPI `FileResponse` + the compat router serve assets.
- No native mobile app and **no Web Share Target / background upload** (Safari can't) —
  capture is always open-app → pick/take, foreground-only resumable upload.
- Satellites are NOT migrated to the clean API in this plan — they keep working
  unchanged against the compat layer; their migration is a **post-cutover** effort.

**Hard constraints (load-bearing, learned the hard way).**
1. **OIDC public-URL rule:** token exchange and JWKS go to `https://auth.discomarder.live/...`,
   **never** `http://authelia:9091` — the documented X-Forwarded-Proto 400 / auth-method 401
   failures came from internal URLs. Use `client_secret_post` + `one_factor` (the iOS-PWA lesson).
2. **Authelia secret single-source:** plaintext secret lives in the stack `.env` only; the
   pbkdf2 hash in Authelia config; never two copies of the same secret.
3. **Saturn is thermally bound, not disk bound:** all three transcode layers apply —
   `cpu_shares: 512` + `mem_limit: 4g`, app-level `FFMPEG_THREADS=1` + concurrency 1, and
   proc-watchdog (already watches ffmpeg). Monitor `/sys/class/thermal`, stay <70 °C on the
   first real batch. Reuse existing derived mp4s on migration — **do not** mass re-transcode.
4. **Compat contract is derived from CONSUMER source, not API recon** — the highest-risk
   seam. Daily-mail renders memo `content` into the email body; satellites extract uids from
   attachment/memo `name`; the transcoder keys state by memo `name` (`memos/{id}`). Omitting
   any of these breaks a satellite *silently at cutover*. Back with fixture contract tests +
   3-satellite byte-identical dry-runs.
5. **Disk layout stays byte-identical** (`assets/{thumbs,mobile,web}/{uid}.*`) — daily-mail
   reads derived tiers off a read-only bind mount, so the layout (not the HTTP tiering) is
   the real dependency.
6. **Saturn protected-path writes:** write to `/tmp` then `sudo cp` (heredoc-through-`tee`
   has produced 0-byte files); sudo password from `/volume1/docker/.sudo_pass`.
7. **Mirror rule:** every live Authelia/Traefik config change is made on Saturn AND in the
   sanitized in-repo template in the same session.

**Terminology locked.** *memos-ng* = the project/repo/stack. *uid* = the stable
public id preserved from old memos (memos and attachments both have one). *clean API* =
the new `/api/*`. *compat layer* = read-only `/api/v1/*` + `/file/*` reproducing memos
0.29 shapes for unmigrated satellites. *fast lane* = sub-second poster/thumb/blurhash
jobs. *slow lane* = the H.264 transcode. *archive master* = the untouched camera
HEIC/HEVC original (ADR-0008); only display/stream tiers are derived.

---

## Goal

Rebuild Memos as **memos-ng**, a first-class custom app on the household default web
stack, keeping the Memos *concept* (capture-first daily notes: markdown, tags, media,
timeline) while owning the codebase so the chronic derived-media pain (5-minute polling
transcoder, nginx rewrite hacks, sub_filter spinner injection, direct-DB resizer)
becomes a designed-in async media pipeline with proper pending states. Become the new
reference project for the default stack.

## Approach

One new repo (`~/projects/memos-ng`), two runtime containers, one image.

```
                         Traefik (websecure, cf)
                                  │
            memos-ng.discomarder.live (→ memos.discomarder.live at cutover)
                                  │
              ┌───────────────────▼───────────────────┐
              │  memos-ng-app (FastAPI, port 8000)     │ traefik_net + internal + postgres_net
              │  · serves SPA (frontend/dist)          │
              │  · /api/* clean REST                    │
              │  · /api/v1/* + /file/* compat router    │
              │  · /api/events SSE (job status)         │
              │  · BFF OIDC (session cookie)            │
              └──────┬──────────────────┬──────────────┘
                     │ postgres17       │ media_jobs table (queue)
              ┌──────▼──────┐    ┌──────▼──────────────────────┐
              │ db memosng  │    │ memos-ng-worker (same image, │ internal + postgres_net
              └─────────────┘    │ command: worker)             │ cpu_shares 512, mem_limit 4g
                                 │ · pyvips: thumbs/mobile/      │
                                 │   blurhash                    │
                                 │ · ffmpeg: poster + H.264 mp4  │
                                 └──────────────────────────────┘
        Shared volume: /volume1/docker/memos-ng → /app/data
        data/originals/…  data/assets/{thumbs,mobile,web}/{uid}.*  (layout identical to old memos)
```

**The media pending-state design (the core fix).** Progressive disclosure, three stages
per video: T+0 upload completes → `attachment status=pending`, UI renders a blurhash/
skeleton placeholder with an indeterminate ProgressBar; memo is postable immediately.
T+~5s fast lane → poster frame (`ffmpeg -ss … -frames:v 1`) + blurhash, status
`processing`, UI swaps shimmer → real poster with live % (parsed from `ffmpeg -progress`,
pushed over SSE). T+done slow lane → full H.264/AAC mp4 written atomically (tmp+rename,
poster-before-video order), status `ready`, SSE swaps in `<video poster=…>`. On `failed`:
poster stays, retry affordance, max 3 retries with backoff — **never a silent drop**.
Images get the same treatment compressed in time (blurhash inline <100 ms via pyvips;
thumb/mobile/web tiers via the fast lane).

**UX port from Memos** (taken over, not reinvented): persistent left sidebar (Home,
Explore, Archived, Resources, Settings + tag tree with counts + calendar heatmap),
capture-first markdown editor pinned atop the Home timeline, infinite-scroll day-grouped
memo cards, image grids with lightbox, tag chips, visibility selector
(PRIVATE/PROTECTED/PUBLIC), search/filter bar — idiomatic PrimeVue 4 (Aura preset via
`definePreset`, dark mode `.app-dark`) on the boat-ctl skeleton.

## Pre-Build Gates (close before the dependent task starts)

| Gate | Blocks | What | Status |
|------|--------|------|--------|
| **G0** | T1.2 docs | Confirm ADR-0002 sanctions app-managed OIDC for mobile-capable apps. | **CLOSED** — verified ADR-0002 (1429) does; ADR-0005 records the BFF/no-middleware refinement. |
| **G1** | T4.1 | Capture live memos 0.29.1 API fixtures (memo list, attachment metadata, `/file/` responses, the `tag in [...]` filter) **before old memos is touched** — they are the compat contract tests' ground truth. | **CLOSED** — captured 2026-06-20 (prod live) to `fixtures/live-0.29.1/` (see `NOTES.md`). Surfaced two contract deviations → **G4, G5**. |
| **G2** | T4.2 sizing | Verify migration counts (87/515/2, ~23 GB) and the API-req recon against the live Saturn DB. Plan is built on the user's word. | OPEN — verify during Phase 0 (read-only psql), not a hard blocker for scaffold. |
| **G3** (hygiene) | T6.1 | When the `memos-ng` shelf is created, re-tag page 1646 from `project=portainer-stacks` to `project=memos-ng`, or it orphans from its own project. | OPEN — Phase 6. |
| **G4** | T4.1 | **Auth-behavior decision (from G1/F1).** Live `/api/v1/memos` with no/bad token returns **HTTP 200 with PUBLIC-only** memos, **not 401** (plan T4.1 had assumed 401). Decide: does the compat layer reproduce 200/PUBLIC-only (matches reality; a dead satellite token silently narrows results) or enforce 401? Confirm what the transcoder actually does on a missing token before choosing. | OPEN — decide before T4.1 code. |
| **G5** | T4.1 | **Original-file serving — DECIDED 2026-06-20: NO originals over the `/file/*` compat HTTP path.** Compat `/file/*` reproduces today's derived-tier-only behavior (`?thumbnail=true` → thumb, mobile-UA → mobile, video → web; original `/file/...` stays 404 as today). Originals are reachable **only via the authed clean API on explicit request** — `GET /api/attachments/{uid}/original` (T2.4) and bulk export `tier=original` (T1.5), both session/PAT-gated. Contract test encodes the derived-tier URLs satellites consume. | **CLOSED** — decision recorded. |

## API Design Requirements (from consuming the live 0.29.1 API at scale)

The clean `/api/*` MUST fix these live-API gaps (the compat router still reproduces the
*old* shapes for unmigrated satellites — these govern the **new** API only):

1. **First-class filtering** (highest impact): server-side `created_before`/`created_after`
   (ISO 8601), repeatable `tag`, `visibility`, `owner`, all composable. The live API has
   **no date filtering** and broken tag filtering. (T1.3)
2. **Standard sort:** `sort=created_at&order=asc|desc` or `sort=-created_at` — not memos'
   `orderBy=create_time+asc`. (T1.3)
3. **`totalCount` in every list response** alongside the cursor. (T1.3)
4. **Explicit URL fields** in attachment metadata: `downloadUrl` + per-tier `thumbnailUrl`/
   `previewUrl`/`webUrl`; drop the always-empty `content`. (T2.4)
5. **Image format negotiation** (WebP/AVIF via `Accept` or `?format=`), serving
   pre-generated variants — never re-encode on the request path. (T2.2/T2.4)
6. **Bulk export** `GET /api/export` (streamed zip) — replaces "454 images = 454 requests". (T1.5)
7. **Field hygiene:** drop dead fields (`externalLink`, `motionMedia`, attachment `content`,
   `snippet`, `property.has*`); add `totalSize` per memo + serialize `width`/`height`/`duration`. (T1.3/T2.4)
8. **Auth-required-by-default:** every `/api/*` and asset route needs a session cookie or
   PAT; PUBLIC is an explicit per-memo opt-in; derived assets stay behind the
   unguessable-uid model (ADR-0004). (T1.3/T2.4 → ADR-0005)
9. **Preserve original capture time** on download/export: store `captured_at` (EXIF
   `DateTimeOriginal` / QuickTime `creation_time` → `File.lastModified` → upload time);
   set HTTP `Last-Modified` and **stamp each export-zip entry's mtime from `captured_at`**;
   propagate to derived tiers; migration copies with `cp -p`. (T1.1/T2.1/T2.4/T1.5/T4.2)

## Client Platform Constraints (iOS Home-Screen PWA — the primary client)

**Build for:** installable PWA (manifest `display: standalone`, full icon set,
`apple-touch-icon`, `apple-mobile-web-app-*` meta, `viewport-fit=cover` +
`env(safe-area-inset-*)`); app-shell service worker (instant cold-launch + Web Push
vehicle); durable **rolling httpOnly** session cookie + silent server-side token refresh
(iOS ITP evicts script-writable storage at ~7 days; a server-set httpOnly cookie is
exempt — the decisive reason BFF beats browser-stored tokens); **Web Push** on
processing-complete (iOS 16.4+, home-screen-installed only).

**Accept, don't fight:** no Background Sync/Fetch → **resumable chunked upload +
Screen Wake Lock** during transfer (foreground-only); no Web Share Target → capture is
always open-app → pick/take; HEIC/HEVC originals kept as archive master, timeline renders
*derived* tiers (ADR-0008); constrained memory → server computes sha256/blurhash/dims,
never read a 100 MB+ file into JS.

## Engineering Standards (2026 reference-grade → ADR-0009)

- **End-to-end type safety:** FastAPI OpenAPI schema is the single source of truth;
  frontend client + types are **generated** (`openapi-typescript` + `openapi-fetch`);
  `useApi` wraps the generated client — no hand-written request/response types; CI fails
  on drift.
- **Server-state via TanStack Query (Vue Query):** timeline = `useInfiniteQuery`;
  create/edit = optimistic `useMutation`; the SSE `useMediaEvents` channel patches/
  invalidates the query cache. Composables stay the convention for app/UI state (refines,
  doesn't reverse, "no Pinia").
- **CI/CD + supply chain:** GitHub Actions — pytest + vitest + tsc + ruff + pyright gate
  every push; build & push GHCR image; **Trivy** scan (fail HIGH/CRITICAL); **SBOM** (syft);
  **Renovate** for npm/pip.
- **Python toolchain:** **uv** + **ruff** + **pyright**; base **python:3.13-slim**.
- **Web security:** strict CSP + HSTS/X-Content-Type-Options/Referrer-Policy/`frame-ancestors`
  with the SPA; rate-limit auth + upload routes.
- **Observability:** structlog JSON + `prometheus-fastapi-instrumentator` from day one.

## Rejected Alternatives

- **Fork/patch upstream memos (Go)** — the pain points are upstream design decisions; a
  permanent Go fork, no reference-project value (WUD fork overhead proven costly w/ bookstackng).
- **Keep Go backend, custom frontend only** — the media problem lives in the backend.
- **Full memos v1 API as the primary API** — inherits visibility-filtering + silent CEL
  failures into the clean reference; compat layer gives satellite stability without the debt.
- **Keep nginx tiering container** — existed to rewrite around an app we didn't control;
  FastAPI `FileResponse` serves assets directly at family scale.
- **imgproxy on-the-fly fallback** — queue-guaranteed pre-generation makes it dead code.
- **Public SPA + PKCE (bookstackng pattern)** — tokens in the browser, JWKS in every
  consumer; BFF gives httpOnly cookies + one token boundary.
- **Merkur-hosted worker** — cross-host NFS + polling caused most historical incidents;
  load (1–2 videos/day) fits Saturn's thermal envelope with three-layer protection.
- **Pinia** — composables are the convention; nothing needs global-store semantics.
- **Celery/Redis queue** — a Postgres `media_jobs` table with `FOR UPDATE SKIP LOCKED`
  suffices, zero extra containers. *Tradeoff:* hand-rolls retry/visibility a dedicated
  runner gives free — acceptable at this scale, revisit if volume grows.
- **Nuxt (SSR)** — fully-authenticated SPA, no SEO/SSR; Vite SPA is simpler, FastAPI owns
  the API + serves `dist/`.
- **Hand-written API types / hand-rolled fetch state** — types generated from OpenAPI;
  server-state is TanStack Query.

---

## Phased Breakdown

> Branch discipline: each task on `feature/<task-slug>` (never main). Each task ends at an
> objective Verification gate. "Do NOT" lines cut scope creep.

### Phase 0 — Scaffold & Infrastructure Prep

**T0.1 — Scaffold `~/projects/memos-ng` from the boat-ctl skeleton.**
`frontend/` (Vue 3.5, TS, Vite, PrimeVue 4.5 + `@primeuix/themes` Aura, Tailwind v4 +
`tailwindcss-primeui`, vue-router, **Vue Query**, vitest/happy-dom, ESLint flat config)
and `app/` (FastAPI, SQLAlchemy 2 async, Alembic, pydantic-settings, structlog,
prometheus-instrumentator). **uv + ruff + pyright.** OpenAPI→client codegen step generates
`frontend/src/api/`. Vite dev proxy `/api`,`/file`,`/health` → :8000. Multi-stage
Dockerfile (node:22-slim → python:3.13-slim, non-root, `linux/amd64`), entrypoint
(alembic upgrade head → uvicorn) + `command: worker` branch.
*Do NOT* hand-write API types. *AC:* `npm run dev` + uvicorn serve a PrimeVue hello-page
through the proxy; `docker build` OK; `/health` 200 with DB check; ruff/pyright/tsc clean;
generated client type-checks against a sample endpoint.

**T0.2 — Provision postgres17 DB `memosng`** (ADR-0001). `CREATE USER/DATABASE` via
`docker exec postgres17 psql` on Saturn; password via `openssl rand` into stack `.env`
(gitignored) + `.env.example`. *AC:* psql connect as `memosng` from a container on
`postgres_net` succeeds.

**T0.3 — Register Authelia OIDC client `memos-ng`.** Live config on Saturn (/tmp + `sudo cp`)
AND sanitized template in the same session (mirror rule). `client_secret_post`,
`one_factor`, `redirect_uris: ['https://memos-ng.discomarder.live/api/auth/callback']`
(+ `memos.discomarder.live` at cutover), scopes `openid profile email groups`,
`response_types: [code]`, `grant_types: [authorization_code]`, `require_pkce: true`.
pbkdf2 hash in Authelia, plaintext in stack `.env` only. The `memos-ng` client is an
**additive** new list item — do **not** touch the existing `memos` client block (verified
additive 2026-06-20: live `memos` client unchanged). *Risk:* Authelia loads the whole config
atomically, so a restart with a bad config takes down **all** household SSO, not just memos.
**Hard gate: run `authelia validate-config` (or `docker exec authelia authelia validate-config`)
BEFORE the restart; restart only on a clean validate, in a quiet window.** *AC:* validate-config
passes; `.well-known/openid-configuration` lists issuer; Authelia logs clean; existing `memos`
client still resolves; client in template with `CHANGE_ME_*` placeholder.

**T0.4 — Create `stacks/memos-ng/`, deploy skeleton at `memos-ng.discomarder.live`.**
`memos-ng-app` (traefik_net + internal + postgres_net; standard labels, **NO
`chain-authelia@file`** — app-managed OIDC per ADR-0002; `autoheal=true`; curl healthcheck
`/health` on `127.0.0.1`; `wud.watch=false` while locally built Phases 0–4) and
`memos-ng-worker` (internal + postgres_net, `cpu_shares: 512`, `mem_limit: 4g`, no Traefik).
Volume `/volume1/docker/memos-ng:/app/data` (create as `<nas-user>`). Dirs `originals/`,
`assets/{thumbs,mobile,web}/`. *AC:* skeleton SPA reachable via https, both containers
healthy, worker idles on empty queue.

**T0.5 — CI/CD + supply-chain from the first commit.** GH Actions: backend pytest+ruff+
pyright; frontend vitest+tsc+lint + **OpenAPI→client drift check**; on main, build & push
`ghcr.io/fmaass/memos-ng` (digest-pinned per ADR-0003), **Trivy** (fail HIGH/CRITICAL),
**SBOM** (syft). **Renovate** for npm+pip. *AC:* green pipeline produces a scanned, SBOM'd
image; a deliberately-stale client fails the drift check; Renovate opens a dep PR.

### Phase 1 — Backend Core

**T1.1 — Data model + Alembic baseline.** SQLAlchemy 2.0 `Mapped` models: `users`
(subject = Authelia preferred_username, display_name, email, role); `memos` (id, **uid
preserved**, owner, content, visibility enum, pinned, created/updated, `archived_at`);
`tags` + `memo_tags` (parsed from `#tag` at save, mirrored for counts); `attachments`
(id, **uid preserved**, memo_id nullable, filename, mime, size, width/height/duration,
`blurhash`, **`captured_at`**, `status` enum **uploading**/pending/processing/ready/failed,
storage path); `uploads` (resumable state for T2.1); `media_jobs` (attachment_id, lane
fast/slow, state, attempts, progress_pct, error, claimed_by, ts); `sessions` (BFF: sid,
user, encrypted tokens, issued_at + rolling last_seen + absolute_expiry); `pats` (hashed,
name, user, last_used); `push_subscriptions` (user, endpoint, p256dh+auth, created_at).
*Risk:* missing a satellite-relied field — cross-check the recon list before freezing.
*AC:* `alembic upgrade head` from empty DB; round-trip tests; uid columns unique-indexed;
`uploads` + `push_subscriptions` present.

**T1.2 — BFF OIDC: login, callback, session, logout.** `/api/auth/login` → 302 to Authelia
authorize (state + PKCE verifier in short-lived cookie); `/api/auth/callback` exchanges
code at **`https://auth.discomarder.live/api/oidc/token`**, validates ID token against
**`https://auth.discomarder.live/jwks.json`**, upserts user, creates session row, sets
httpOnly Secure SameSite=Lax cookie; `/api/auth/user`; `/api/auth/logout` (kill session +
RP-initiated logout). Auto-provision on first login. Deps `require_user` (all `/api/*`
except health/metrics/auth), `require_pat_or_user` (compat). **Security:** CSP + HSTS/
X-Content-Type-Options/Referrer-Policy/`frame-ancestors 'none'`; rate-limit login/callback.
**Durable login:** rolling cookie (absolute cap ~90 days, `last_seen` bumped), refresh
token server-side + silently refreshed. *Note (G0 closed):* ADR-0002 sanctions this; record
BFF/no-middleware specifics in ADR-0005. *Greenfield warning:* first true no-middleware
confidential BFF here — budget extra. *AC:* full browser round-trip on the deployed
skeleton; cookie httpOnly; `curl /api/memos` no cookie → 401 JSON; session survives a
refresh cycle without re-login.

**T1.3 — Memo CRUD + timeline + search API.** Every endpoint fully typed in the OpenAPI
schema (Pydantic v2, no untyped dicts). `/api/memos` GET (cursor pagination created_at
desc, **`totalCount`**), POST; `/api/memos/{id}` GET/PATCH/DELETE (soft archive + hard
delete); pin/unpin; `/api/tags` (counts); `/api/stats/heatmap`. **Filtering (#1):**
`created_before`/`created_after`, repeatable `tag`, `visibility`, `owner`, any combo +
Postgres `tsvector` full-text. **Sort (#2):** `sort`/`order`. **Field hygiene (#7).**
Visibility: PRIVATE owner-only, PROTECTED any authed, PUBLIC opt-in; **auth-by-default (#8)**.
*Do NOT* add Meilisearch — tsvector simple config. *AC:* pytest covers CRUD, cursor
stability + `totalCount`, date/tag/combination filters (regression guards), sort both
directions, visibility matrix (owner/other/PAT), tag counts; tests fail if visibility check
removed; schema asserts dead fields absent.

**T1.4 — PAT issuance + management.** `/api/pats` CRUD (token shown once, stored sha256),
`Authorization: Bearer` on compat + clean API, per-PAT last_used. *AC:* PAT authenticates
`GET /api/v1/memos`; revoked PAT → 401.

**T1.5 — Bulk export `GET /api/export` (#6).** Same filter grammar as `/api/memos` + `tier`
(`original|web|thumbnail`); `StreamingResponse` zip (no full in-memory buffer) + manifest;
**each entry's mtime = `captured_at` (#9)**; session/PAT auth; cap concurrent exports (1–2);
max-bytes/max-count guard → 413. *AC:* `?tag=curated&tier=web` returns a valid streamed zip
of exactly the curated web assets; unzipped mtimes = `captured_at`; unauthenticated → 401;
oversized unfiltered → 413 with guidance.

### Phase 2 — Media Pipeline

**T2.1 — Resumable chunked upload + original storage** (Client-Constraints; ADR-0007).
Offset-based protocol (tus-compatible shape): `POST /api/uploads` (declared
filename/mime/size + client `File.lastModified` → `upload_uid` + chunk size);
`PATCH /api/uploads/{uid}` appends at `bytes_received` (idempotent on re-send), streams to
a part file, updates running sha256; `HEAD /api/uploads/{uid}` returns the offset for
resume; `POST .../finalize` assembles → `data/originals/{yyyy}/{mm}/{uuid}_{origname}`,
verifies size/sha256, sha256 dedup, mime sniff, promotes to an `attachment`. **Server
computes sha256/blurhash/dims and `captured_at`** (EXIF/QuickTime → `File.lastModified` →
upload time). Lifecycle: `uploading` from first chunk (memo postable, card shows %);
finalize → images `processing`, videos `pending` (fast + slow lanes enqueued). Client holds
**Screen Wake Lock** during transfer. Abandoned `uploads` reaped on expiry. *Risk:* Traefik
body-size/read-timeout on `PATCH` — generous limits, modest chunks (5–10 MB); atomic
tmp+rename assembly. *AC:* a 200 MB .mov over a throttled/interrupted link **resumes from
the last offset** (kill mid-transfer, re-HEAD, continue) and finalizes with matching sha256;
`uploading`→`pending` with job rows; duplicate finalize returns the existing attachment;
abandoned upload reaped.

**T2.2 — Worker: queue claim loop + fast lane.** Same image, `python -m app.worker`;
`SELECT … FOR UPDATE SKIP LOCKED` on `media_jobs`, concurrency 1, LISTEN/NOTIFY wake
(fallback 15 s poll). Images → decode HEIC original (libvips/libheif) → pyvips thumbs
(800px q80) + mobile (2048px) + optional WebP/AVIF; **HEIC original untouched (ADR-0008)**.
Videos → `ffmpeg -ss 0.5 -frames:v 1` poster + blurhash. Atomic tmp+rename; derived tiers
carry capture metadata (pyvips keeps EXIF; ffmpeg `-map_metadata 0`), file mtime =
`captured_at`. *AC:* upload → thumbs/mobile/poster on disk within 10 s; jobs `done`;
`kill -9` mid-job → re-claimed after visibility timeout, no corrupt files.

**T2.3 — Worker: slow lane (H.264) + progress + thermal guards.** ffmpeg H.264/AAC mp4
(`-threads 1`, faststart, ~1080p) to `web/{uid}.mp4` — **derived stream copy only; HEVC
original retained (ADR-0008)**. tmp+rename, poster-before-video order. Parse `-progress
pipe:` → `media_jobs.progress_pct` (throttled ~2 s) → NOTIFY → SSE. Retry max 3 w/ backoff;
`failed` is terminal + UI-visible. `nice`/`ionice` + proc-watchdog coverage. *Risk:* thermal
— **monitor `/sys/class/thermal`, <70 °C on the first batch**. *AC:* .mov → playable mp4
within minutes; progress visible in DB during run; corrupt input → 3 attempts → `failed`
with message; Saturn temps logged.

**T2.4 — SSE status channel + asset serving.** `GET /api/events` (SSE, session-auth)
relaying Postgres NOTIFY for the user's attachments. Asset routes
`/api/attachments/{uid}/original|thumb|mobile|web|poster` via `FileResponse` with ETag/Range
(needed for `<video>`) + **`Last-Modified` = `captured_at` (#9)**. Metadata embeds
`downloadUrl` + per-tier URLs (#4), no empty `content`. **Format negotiation (#5)** (serve
pre-generated variants). **Auth-by-default (#8)** (ADR-0004 governs PUBLIC exposure). *Risk:*
SSE through Traefik needs buffering off — verify; 5 s polling fallback in the composable.
*AC:* EventSource shows uploading→pending→processing(p%)→ready live; video seeks (206);
attachment JSON has working tier URLs; `Accept: image/webp` returns WebP; unauthenticated
asset → 401.

**T2.5 — Web Push "processing complete"** (Client-Constraints). VAPID keypair in stack
`.env`; `POST /api/push/subscribe` stores a `push_subscriptions` row; the **worker** sends
(pywebpush) on terminal transitions — `ready` / `failed` — reusing the SSE NOTIFY source.
Best-effort/opt-in; gate the prompt on `display-mode: standalone`; prune 410 Gone. *AC:* on
a real iPhone home-screen install, uploading a video and locking the phone yields a push on
completion; tap deep-links to the memo; revoked subscription pruned.

### Phase 3 — Frontend (the Memos UX, in PrimeVue)

**T3.1 — App shell: sidebar nav, theming, auth guard, PWA.** Sidebar (Home, Explore,
Archived, Resources, Settings) + tag tree w/ counts + calendar heatmap (custom CSS grid).
Aura `definePreset`, dark-mode toggle, Toast + Confirm. **Vue Query provider at root**;
`useApi` is the fetcher. Router guard → `/api/auth/login` on 401 via `useSession`. Responsive
drawer on mobile. **PWA:** manifest (`display: standalone`, maskable icons), apple meta tags,
`viewport-fit=cover` + safe-area padding; app-shell SW (vite-plugin-pwa; versioned precache +
skipWaiting/clientsClaim; **never cache `/api/*`**) doubling as the Web Push receiver
(notificationclick → deep-link); `useStandalone` composable gating iOS affordances. *AC:*
nav matches memos side-by-side; dark mode persists; deep-link while logged out → login →
lands on the memo; "Add to Home Screen" yields a standalone launch with correct name/icon, no
Safari chrome; safe areas respected on a notched iPhone.

**T3.2 — Capture editor.** Pinned card atop Home: markdown textarea + toolbar
(bold/italic/code/checkbox/link), `#tag` AutoComplete (from `/api/tags`), visibility
SelectButton, attachment zone (drag-drop + paste + iPhone camera roll / `capture`), Save →
optimistic prepend. Edit reuses the component in a Dialog. **Video upload first-class:** the
zone drives the **resumable client (T2.1)** — per-file %, auto-resume, held Wake Lock +
"keep the app open" affordance; **memo postable while a video still uploads**; bounded-
concurrency queue for multi-file. *Do NOT* add a WYSIWYG library. *AC:* create/edit memo with
tags + 3 iPhone-Safari images; paste screenshot works; tag count increments; a video uploads
with visible %, the memo posts immediately, and the upload resumes after brief backgrounding.

**T3.3 — Timeline + memo cards + media rendering with pending states.** Infinite scroll via
**`useInfiniteQuery`** + IntersectionObserver; create/edit are optimistic `useMutation`s;
**`useMediaEvents` (SSE) patches/invalidates the cache**. Day-grouped headers; card =
sanitized markdown (markdown-it), tag chips, responsive 1/2/3-up image grid w/ Galleria
lightbox, video block. **Media states:** `uploading` (upload %), `pending` (blurhash +
shimmer + "Processing" Tag), `processing` (poster + circular % overlay), `ready`
(image→lightbox / `<video poster>`), `failed` (poster + warning + retry). Checkbox-task lines
toggle via PATCH. *Risk:* markdown XSS — strict allowlist sanitizer. *AC:* upload a video,
watch one card pass all 4 states without reload; screenshot for the wiki; `<script>` memo
renders inert.

**T3.4 — Explore, Archived, Resources, Search.** Explore = non-private memos across users;
Archived = list + restore; Resources = attachment grid (DataView, type filter, orphan
indicator); Search = debounced top bar, server tsvector, filter chips
(tag/date/visibility/has:video). *AC:* each view works against seeded data; search returns
expected memos; archived memo absent from Home, present in Archived.

**T3.5 — Settings: profile, PATs, members.** Profile (display name); PAT UI (create/name/
revoke, one-time copy); members (read-only, from Authelia); instance info
(`__APP_VERSION__`). *AC:* create PAT in UI, use via curl against compat API, revoke → 401.

### Phase 4 — Compat Layer & Migration

**T4.1 — Memos-compat read API** (contract derived from CONSUMER source, verified 2026-06-19).
`compat.py`: `GET /api/v1/memos` reproducing memos 0.29 exactly as consumed —
**Envelope:** `memos` array key + `nextPageToken` (loop terminates on absence). **Memo:**
`name` (`memos/{id}` — transcoder state `memo-map.json` is keyed by this), `uid`,
`createTime`, `visibility`, `tags`, **`content`** (daily-mail renders it into the email body).
**Attachment** (`attachments[]`): **`name`** (`attachments/{uid}` — satellites
`split("attachments/")[-1]`), `filename`, `type` (mime), **`size`**. **Auth (per G4 — verified
2026-06-20, NOT the original 401 assumption):** no/bad token returns **HTTP 200 with PUBLIC-only**
memos; Bearer PAT widens to PROTECTED/PRIVATE. Reproduce the live 200/PUBLIC-only behavior (or
resolve G4 first) — a 401 would change the transcoder's failure path. **Filter:** CEL
`filter=tag in ["curated"]` (slideshow — the working list form, distinct from the `tag==` that
400s). **File serving (per G5 — DECIDED 2026-06-20: no originals over `/file/*`):** reproduce today's
derived-tier-only behavior — `?thumbnail=true` → thumb, mobile-UA → mobile, video ext → web; the
original `/file/attachments/{uid}/{filename}` path stays **unserved** (404, as prod is today).
Originals are obtainable **only via the authed clean API on explicit request** —
`GET /api/attachments/{uid}/original` (T2.4) or export `tier=original` (T1.5), never over the
public `/file/*` compat path. **daily-mail reads derived tiers off its disk mount**, so the byte-identical disk layout is
the real dependency regardless. Contract tests from
**recorded live fixtures (G1)**. *AC:* contract tests green for the full field set;
**daily-mail, slideshow, transcoder each dry-run against staging and produce byte-identical
output** to a prod run on the same data.

**T4.2 — Migration ETL: old memos DB + files → memos-ng.** Idempotent
`app/scripts/migrate_from_memos.py` reads old `memos` DB read-only over postgres_net:
`user`→users (map to Authelia subjects — confirm both humans), `memo`→memos (uid, content,
visibility, timestamps, pinned, parse tags), `attachment`→attachments (uid, copy file into
`originals/`, mime, link). Derived assets: **copy `assets/{thumbs,mobile,web}` as-is**
(keyed by uid — zero re-transcode), create `done` job rows + backfill blurhash from existing
thumbs. Videos without an existing mp4 (PRIVATE blind spot + never-transcoded backlog) →
enqueue slow-lane **throttled + temp-monitored**. Backfill `captured_at` from EXIF/QuickTime
(#9). Copy via Saturn-local **`cp -p`** (preserve timestamps), not network. *Risk:* 23 GB
local cp (fine); broken file refs → log + skip + report. *AC:* dry-run prints counts matching
recon (87/515/2); spot checks (oldest, newest, a video, a `#curated` memo) render correctly;
re-run is a no-op.

### Phase 5 — Cutover & Decommission

**T5.1 — Pre-cutover parity verification (staging).** With migrated data: Playwright over
golden paths (login, timeline scroll, create memo w/ photo, upload video → full
uploading→pending→ready, search, tag nav, archive, lightbox, mobile viewport); compat
contract tests vs live fixtures. **On-device iOS PWA pass (both iPhones):** Add-to-Home →
standalone cold-launch → OIDC round-trip returns to the app (not Safari) → identity;
**first-class video upload: real 4K video uploads with %, survives backgrounding mid-transfer
and resumes, transcodes, Web Push fires**; safe-area layout correct. *Risk:* parity gaps
surface here by design — budget a fix iteration. *AC:* golden-path checklist signed off;
screenshots archived; both iPhones verified (install + login + resumable upload + push).

**T5.2 — Cutover: hostname, satellites, freeze window.** (1) announce freeze, stop old-memos
writes (scale memos→0, reversible); (2) re-run ETL delta sync (idempotent); (3) move Traefik
rule: memos-ng takes `memos.discomarder.live` (+ keep memos-ng.* alias), add memos.* redirect_uri
to the Authelia client (live + template); (4) repoint satellites (`MEMOS_BASE_URL`/token for
daily-mail, slideshow, rme, rausmitdir → new host + fresh PATs; daily-mail disk mount →
`/volume1/docker/memos-ng`); (5) daily-mail dry-run + slideshow one-shot to verify; (6) keep
old stack stopped-but-present 7 days. *Risk:* highest blast radius — every step has rollback
(old stack restart + Traefik revert ≤ 5 min); schedule outside the 07:00 Cronicle window.
*AC:* memos.discomarder.live serves memos-ng; both users logged in; next morning's daily mail
correct; slideshow green; old containers stopped.

**T5.3 — Decommission old pipeline.** After 7 quiet days: remove old `stacks/memos/`
(memos, memos-nginx, memos-imgproxy, memos-resizer), retire Merkur
`merkur-stacks/memos-transcoder/` + NFS mount + Cronicle entries, archive
`/volume1/docker/memos` to backup (**don't delete** — 23 GB family archive; verify memos-ng
copy via checksum sample first), drop old `memos` DB after final pg_dump, remove old Authelia
`memos` client (live + template), update Uptime Kuma. *AC:* WUD/Uptime Kuma/Cronicle show no
old-memos references; backup dump exists; ADR + wiki updated.

### Phase 6 — Reference-Project Designation & Documentation

**T6.1 — ARCHIMEDES docs for memos-ng.** New shelf `memos-ng` via /archimedes-review:
Project Overview, feature pages (BFF OIDC Auth, Media Pipeline & Pending States, Compat Layer,
Timeline UI, Migration ETL), Operations runbook (stuck-job recovery, failed-transcode retry,
thermal response), source maps. Update stale page 898 → superseded pointer. **Re-tag page 1646
to `project=memos-ng` (G3).** Update both CLAUDE.md "Documented Projects" tables. *AC:*
/archimedes-health for memos-ng green; page 898 resolved.

**T6.2 — ADRs 0005–0009.** 0005 (memos-ng replaces upstream memos; BFF OIDC = reference auth
for custom apps, refines ADR-0002; clean API auth-by-default + first-class filtering; httpOnly
rolling cookie chosen to survive iOS ITP). 0006 (in-process Postgres job queue + in-stack
worker = default async pattern; ADR-0004 carry-forward). 0007 (resumable chunked +
foreground-only upload = the iOS-PWA upload pattern). 0008 (keep camera HEIC/HEVC originals as
archive master). 0009 (default-stack engineering standards — OpenAPI types, TanStack Query,
CI/CD+Trivy+SBOM+Renovate, uv/ruff/pyright on 3.13, CSP). *AC:* all five accepted + tagged.

**T6.3 — Declare reference status.** Update `~/CLAUDE.md` / conventions: memos-ng is the
canonical default-stack reference (auth: memos-ng BFF; uploads+async: memos-ng pipeline;
engineering envelope per ADR-0009; boat-ctl remains reference for API-token ingest); note in
NEW_SERVICE_SETUP_CHECKLIST.md. *AC:* docs updated; grep finds no remaining "boat-ctl is the
reference" claims.

---

## Critical Path & Sequencing

```
G0✓ ─┐
G2 ──┼─► T0.1 ─► T0.4 ─► (T0.2, T0.3 parallel) ─► T0.5
     │              │
     │              ▼
     │           T1.1 ─► T1.2 ─► T1.3 ─► (T1.4, T1.5 parallel)
     │                              │
     │                              ▼
     │      T2.1 ─► T2.2 ─► T2.3 ─► T2.4 ─► T2.5     (Phase 2 needs T1.1 model + T1.2 auth)
     │                              │
     │                              ▼
     │      T3.1 ─► T3.2 ─► T3.3 ─► (T3.4, T3.5)      (Phase 3 needs T1.3 API + T2.4 SSE)
     │
G1 ──┴─► T4.1 ─► T4.2 ─► T5.1 ─► T5.2 ─► T5.3 ─► T6.1 ─► T6.2 ─► T6.3
```

- **Longest pole:** T1.2 (greenfield BFF OIDC — highest uncertainty) and T2.1↔T2.3↔T3.3
  (the resumable-upload + pending-state loop, the actual reason for the rebuild). Front-load
  a spike on each.
- **G1 must be captured before old memos is touched** — it gates T4.1's contract tests and is
  irreversible if missed. Do it early even though Phase 4 is late.
- Phases 0–4 run entirely on `memos-ng.discomarder.live` (staging) — no production pressure
  until T5.2.
- Independently shippable: each phase ends deployable on staging; Phase 5 is the only
  production-affecting phase.

## Dependencies

**External / state:** GHCR push for `ghcr.io/fmaass/memos-ng` (exists). Quiet windows for the
Authelia restart (T0.3) and cutover (T5.2). **Live 0.29.1 fixtures captured before old memos
stops (G1).** `postgres17` (pgvector 0.8.2-pg17) on `postgres_net`; manual psql provisioning.
Authelia live config `/volume1/docker/authelia/config/configuration.yml` + template
`stacks/traefik-authelia/authelia/configuration.yml.example` (existing `memos` client ~lines
219–234). Old memos: DB `memos` (87/515/2); files `/volume1/docker/memos/` (~23 GB). proc-watchdog
already watches ffmpeg. Satellites: `stacks/daily-dami-memos-mail/`, `merkur-stacks/memos-slideshow/`,
`merkur-stacks/memos-transcoder/` (retire), rme/rausmitdir `MEMOS_BASE_URL`. boat-ctl
(`~/projects/boat-ctl`, skeleton — confirmed present), radioeins-collector
(`~/projects/radioeins-collector`, auth.py/scheduler patterns — confirmed present). Traefik
file rules `/volume1/docker/traefik/rules/`; `traefik_net` macvlan. BookStack pages: 898
(memos, stale), 1456 (transcoder), 1504 (ADR-0004), 1422 (daily mail), 1405 (slideshow), 1429
(ADR-0002), 1646 (this plan of record).

**Inter-task:** T0.1 → everything; T1.1 model → all of Phase 2/most of Phase 1; T1.2 auth →
all authed routes; T2.4 SSE → T3.3; G1 fixtures → T4.1; T4.2 migration → T5.1.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Multi-week, multi-session build | High | Schedule | Phases independently shippable on staging; no prod pressure until Phase 5 |
| Satellite breaks on undocumented API field | Medium | Daily mail fails silently | Source-derived field list + fixture contract tests + 3-satellite dry-runs (T4.1/T5.2) |
| Thermal event during migration backlog transcode | Medium | Saturn emergency shutdown | Reuse existing derived assets (no mass re-transcode); throttled 1-thread queue; temp monitoring |
| SSE through Traefik buffering | Medium | Pending states degrade | Automatic polling fallback in `useMediaEvents` from day 1 |
| BFF OIDC greenfield — first no-middleware app-OIDC here | Medium | T1.2 slips / login bugs | G0 closed (ADR-0002 sanctions); spike first; `one_factor`; on-device test (T5.1) |
| iOS PWA breaks OIDC return-to-app or background-resume | Medium | Can't log in / upload fails | `one_factor` (fewer hops) + resumable upload + Wake Lock; on-device fix loop budgeted (T5.1) |
| Dropping camera originals on a false "lossless" premise | Medium | Irreversible quality loss on irreplaceable media | ADR-0008 keeps originals untouched; archive before any deletion (T5.3) |
| User-mapping mistake in ETL (2 users) | Low | Wrong memo ownership | Explicit mapping table, dry-run prints it, user confirms before live run |
| Rollback needed post-cutover | Low | Data written to memos-ng lost on rollback | 7-day soak (old stack stopped-not-removed); rollback playbook; freeze window minimizes delta |
| Authelia restart disrupts household | Low | Brief login outage | Quiet-window scheduling; config validated before restart |
| Resumable-upload complexity / orphaned part files | Low | Wasted disk, stuck uploads | Simple offset model, atomic assemble, expiry reaper |
| Migration counts unverified locally (G2) | Low | Sizing/ETL surprises | Verify against live Saturn DB in Phase 0 before T4.2 |

## Acceptance Criteria

1. memos.discomarder.live serves memos-ng (Vue 3 + PrimeVue 4 + FastAPI + postgres17 `memosng`); upstream memos containers retired.
2. Login is Authelia OIDC via confidential BFF: tokens server-side only, httpOnly session cookie, token + JWKS via public https URLs, `client_secret_post` + PKCE; both users log in on iPhone and desktop.
3. Video upload UX: posting a .mov shows a placeholder (blurhash/skeleton → poster + live %) within seconds and swaps to a playable mp4 automatically without reload; failures visible with retry — verified live with screenshots.
4. All 87 memos / 515 attachments migrated with uid, timestamps, visibility, tags intact; spot checks pass; derived assets reused (no re-transcode of existing mp4s).
5. Daily Dami Memos Mail, Monthly Slideshow, rme/rausmitdir run green against memos-ng (compat API + identical disk layout) with **zero satellite code changes**.
6. Worker honors all three thermal layers; first real transcode batch <70 °C; failed jobs terminal-and-visible, never silently dropped.
7. ARCHIMEDES shelf `memos-ng` documented (features + operations + ADR-0005/0006); old memos pages superseded; memos-ng declared the default-stack reference in CLAUDE.md.
8. Test integrity: backend pytest (visibility matrix, queue claim semantics, compat contract), frontend vitest — all failing when their feature is broken.
9. Clean API fixes the live gaps (API-req §): server-side date+tag+combination filtering, standard `sort`/`order`, `totalCount`, explicit `downloadUrl`/tier URLs, format negotiation, bulk `/api/export` zip, dead fields dropped, auth-by-default, **original-capture-time preserved on download/export** — each covered by a regression test.
10. Installable iOS home-screen PWA (manifest + apple meta + safe areas + app-shell SW) with **first-class resumable video upload that survives backgrounding/lock and never restarts from zero**, an `uploading→processing→ready` lifecycle, durable rolling login outliving iOS ITP eviction, and Web Push on processing-complete — verified on both family iPhones (T5.1).
11. Camera-media retention per ADR-0008: HEIC/HEVC **original kept unmodified as archive master**; only derived tiers generated; spot-check confirms a migrated and a fresh photo+video each retain their original alongside working derived tiers.
12. Reference-grade envelope (ADR-0009): frontend types **generated from backend OpenAPI** (CI drift-check green), server-state via **TanStack Query**, **CI/CD** gates every change (tests + ruff + pyright + tsc) shipping a **Trivy-scanned, SBOM'd** image with **Renovate** dep PRs, backend on **uv/ruff/pyright + Python 3.13**, SPA serving **strict CSP + security headers** with rate-limited auth/upload routes.

---

## Self-Check Results (archimedes-plan Step 3)

1. **AC clear per task?** Yes — every task carries an objective Verification gate; the 12 plan-level AC are testable.
2. **Implicit inter-task deps?** Surfaced explicitly in Critical Path & Sequencing and Dependencies (T1.1 model, T1.2 auth, T2.4 SSE→T3.3, G1→T4.1, T4.2→T5.1).
3. **Task-N-fails recovery?** T5.2 cutover has an explicit ≤5-min rollback (old stack restart + Traefik revert) + 7-day soak; ETL is idempotent; worker jobs retry 3× then terminal-visible.
4. **Untracked files/modules?** Dependencies enumerates every touched stack, config, satellite, volume, and BookStack page.
5. **Assumed state that may not exist?** Two items pulled into explicit **Pre-Build Gates**: G1 (live fixtures — capture before old memos stops) and G2 (migration counts unverified locally). G0 was an assumed risk — now verified CLOSED against ADR-0002.
6. **Actually multiple plans?** Yes, deliberately — it's `scope: major`, sequenced into 6 independently-shippable phases on staging. Kept as one plan because the phases share one repo/image/contract and only Phase 5 is reversible-critical.
7. **Security/perf/data-integrity?** Addressed: auth-by-default (#8), CSP/rate-limits, unguessable-uid (ADR-0004), thermal guards, atomic writes, archive-master retention (ADR-0008), 7-day soak before any deletion, checksum-verify before archive in T5.3.
8. **Duplicates existing functionality?** It *replaces* existing functionality (old memos + 3 containers + Merkur transcoder) — acknowledged throughout; the compat layer is the deliberate bridge so consumers don't break.

**Outstanding for the user before build starts:** G0 and **G1 are CLOSED** (G1 fixtures captured
2026-06-20 to `fixtures/live-0.29.1/`). G1 surfaced two compat-contract deviations now tracked as
**G4** (auth returns 200/PUBLIC-only, not 401) and **G5** (originals 404 over HTTP; only derived
tiers served) — both are *decisions* to make before T4.1 code, not blockers for scaffold. **G2**
(verify 87/515/2 + ~23 GB against the Saturn DB) remains, verifiable in Phase 0. T0.3 now carries a
hard `authelia validate-config` gate before the SSO-affecting restart. No unaddressed concerns
block the plan; the residual gates are input-verification and two compat decisions, not design.
