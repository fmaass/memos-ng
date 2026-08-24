---
name: "Native iOS client + reference iOS architecture"
overview: "Build a native SwiftUI client for memos-ng that doubles as the household's reference iOS project (the iOS analogue of the FastAPI+Vue default web stack). Backend enablers in memos-ng (bearer auth on every API route, a browser-brokered native device login that leaves Authelia untouched), then the app in a new repo memos-ng-ios, shipped via TestFlight, with the architecture ratified as estate doctrine. App Store (unlisted) is an explicitly user-gated final phase."
scope: major
project: "memos-ng"
feature: "native-ios-client"
status: approved
created: "2026-08-23"
todos:
  - id: p0-decision-records
    content: "Phase 0 — ADR-0015 (memos-ng) + reference-iOS-architecture doctrine draft + book"
    status: pending
  - id: p1-bearer-everywhere
    content: "Phase 1 — PAT bearer accepted on every /api/* route; PAT expiry; openapi drift test; deploy"
    status: pending
  - id: p2-native-login
    content: "Phase 2 — Browser-brokered native device login (PKCE + one-time code → PAT); deploy"
    status: pending
  - id: p3-ios-foundation
    content: "Phase 3 — memos-ng-ios repo: reference skeleton, generated client, paste-PAT login, read-only timeline"
    status: pending
  - id: p4-ios-native-login-writes
    content: "Phase 4 — ASWebAuthenticationSession login; compose/edit/delete; task toggle; markdown"
    status: pending
  - id: p5-ios-share-upload
    content: "Phase 5 — Share extension + background chunked upload"
    status: pending
  - id: p6-testflight
    content: "Phase 6 — App Store Connect setup, Xcode Cloud → TestFlight internal, household on it"
    status: pending
  - id: p7-ratify-doctrine
    content: "Phase 7 — Ratify reference architecture (ADR-0001 memos-ng-ios) after Codex + ChatGPT-Pro reviews"
    status: pending
  - id: p8-unlisted-app-store
    content: "Phase 8 (user-gated) — demo instance + unlisted App Store submission"
    status: pending
isProject: false
---

# Implementation Plan: Native iOS client + reference iOS architecture

## Context for Independent Review

**What memos-ng is.** A FastAPI + SQLAlchemy-2-async + Vue 3/PrimeVue 4 replacement for
`neosmemo/memos` that IS production at `https://memos.discomarder.live` (household, 2 users,
231 memos, 2 PATs, 35 web sessions as of 2026-08-23). It is also the household's **reference
web-stack project** (plan page 1649: "the new default-stack reference project, superseding
boat-ctl"). There is no separate staging; every deploy and every prod migration is
production-affecting (project CLAUDE.md "CARDINAL RULE").

**What this plan adds.** Two things, deliberately coupled: (1) a native SwiftUI iOS client for
memos-ng, and (2) that client's repo, `memos-ng-ios`, becoming the household's **reference iOS
architecture** — the iOS analogue of what memos-ng is for the web stack. The user's framing
(2026-08-23, verbatim): *"plan ahead to make this trial our reference project of the future:
same as with our reference web stack — since we have a default infra structure to speak to we
should also have a reference, industry proven common best practice ios reference architecture."*
The architecture choice is therefore a doctrine decision with estate blast radius, not a
per-app taste call, and gets the review treatment reserved for design artifacts (independent
Codex AND ChatGPT-Pro reviews, run separately; `~/.claude/CLAUDE.md` → Agent Channels).

**Current state that constrains the design (all verified 2026-08-23, file:line from the repo at
`b7f3b68`, v1.4.0):**

- **Auth is a confidential-client OIDC BFF.** `GET /api/auth/login` (`app/routers/auth.py:34`)
  sets an encrypted `memosng_oidc_tx` cookie `{state, verifier, next}` and 302s to Authelia;
  `GET /api/auth/callback` (`:61-91`) exchanges the code, validates the id_token, upserts the
  user and calls `create_session` (`app/auth.py:126-140`), which stores a `sessions` row with
  Fernet-wrapped OIDC tokens and sets the httpOnly `memosng_sid` cookie (SameSite=lax, Secure,
  90-day absolute, rolling refresh throttled to 600 s — `app/config.py:159-165`,
  `app/auth.py:158-192`). Token/JWKS endpoints use the public `https://auth.discomarder.live`
  URLs (`app/config.py:143-154`).
- **Personal access tokens exist but are second-class.** `Pat` model (`app/models.py:359-367`):
  `id, hashed_token, name, user_id, last_used, created_at` — **no `expires_at`, no scopes, no
  revoked flag** (tracked as open ticket MEMNG-34). Tokens are `memos_pat_…` (`app/auth.py:195`),
  SHA-256 hashed at rest (`:198-199`), shown in plaintext exactly once on creation
  (`app/routers/pats.py:29`), managed in the SPA by `frontend/src/components/PatManager.vue`.
  Bearer is accepted ONLY by `require_pat_or_user` (`app/auth.py:351-358`), used in 2 files:
  `app/routers/assets.py` (original/print tier) and `app/routers/compat.py` (read-only
  `/api/v1/*`, `/file/*`). Every other route — memos CRUD, uploads, settings, export, push,
  pats, and the SSE `/api/events` (`app/routers/assets.py:811-826`, which calls
  `auth.require_user(request, db)` directly) — is cookie-only `require_user`
  (`app/auth.py:165-192`). Auth is wired 100 % per-route `Depends(...)`; no router declares
  `dependencies=[...]` (39 call sites across 9 router files).
- **CSRF** is an Origin/Referer middleware (`app/csrf.py`, wired `app/main.py:126`) that engages
  only when the `memosng_sid` cookie is present (`app/csrf.py:100-106`). A bearer request
  without a cookie is exempt by construction.
- **Media** derived tiers are HMAC-signed URLs served with no DB and no auth
  (`app/asset_urls.py:42-46`, `app/routers/assets.py:502-515`; TTL 7200 s bucketed 3600 s,
  `app/config.py:46-47`); `original`/`print` require cookie or PAT. Uploads are tus-style
  chunked: `POST /api/uploads` → `PATCH /{uid}` raw body + `Upload-Offset` → `HEAD` probe →
  `POST /{uid}/finalize` (`app/routers/uploads.py:61-126`; 5 MiB chunks, 1 GiB cap).
- **OpenAPI 3.1.0** is served live and exported to repo-root `openapi.json` by
  `scripts/gen-openapi.py`; the SPA generates `src/api/schema.d.ts` from it
  (`frontend/package.json:14`). A drift step exists in `.github/workflows/ci.yml:76-81`
  but CI is abandoned and never runs (CLAUDE.md; MEMNG-1 cancelled); **no local test exists**
  (`grep -rn openapi tests/` → 0), so nothing enforces the contract today.
- **Markdown** is rendered client-side only (`frontend/src/lib/markdown.ts:10-58`: markdown-it +
  task lists + DOMPurify; tag-only lines stripped, `:24-42`). No server render endpoint.
- **Push** is browser Web Push/VAPID (`app/routers/push.py`) — unusable from a native app.
- **Tests** authenticate via the `logged_in_client` fixture (`tests/conftest.py:99-131`), which
  drives the real OIDC flow with `exchange_code`/`validate_id_token` mocked (no
  `dependency_overrides` — eager routes bake handlers at construction). PAT tests:
  `tests/test_pats.py` (6), `tests/test_pat_last_used.py` (16). Gate = 760 tests green at v1.4.0.
- **Decision records.** No ADR ever locked "PWA-primary"; it lives as plan prose
  (`docs/plans/PLAN_2026-06-20_memos-ng.md:43,97,227` = BookStack page 1649; feature-parity plan
  page 1884). Estate ADR index (page 2697) already says **"OIDC for mobile-capable apps"**
  (portainer-stacks ADR-0002 (estate-scope), `/link/1429`); bookstack-ng ADR-0002 (`/link/1432`) chose
  "server-side OIDC with session cookies, no client-side tokens" for a *browser-only* app — a
  project-scoped precedent this plan must cite, not silently contradict. memos-ng ADRs live in
  BookStack book 1648, chapter 2690 "Decisions", highest ADR-0014 (page 2584) → next is
  **ADR-0015**. ADR numbering is per project; the new iOS repo starts at **ADR-0001**.
- **Estate doctrine form.** The household has NO standalone "reference web stack" doctrine page
  (scout 2026-08-23: 0 hits across 7 search terms + ADR index, positive control confirmed); the
  reference is a *project* that others clone-and-adapt. The iOS reference follows the same form:
  a reference **repo** whose `CLAUDE.md` + `docs/ARCHITECTURE.md` + ADR-0001 carry the doctrine,
  cross-listed in the estate ADR index allow-list.
- **Environment.** The app/worker run on jupiter (local Docker), DB on Saturn
  (`192.168.1.50:5455`). Prod alembic head `8b7d6acb2567` == repo head (15 migrations); running
  image `memos-ng:local` Id `7616161a…` (v1.4.0), started 2026-08-19 04:37 UTC; four
  `rollback-*` tags exist. **merkur** (`ssh merkur.local`) is a
  Mac mini M1 / 16 GB / macOS 26.6 with **no Xcode installed** (Command Line Tools only, a stale
  Swift 5.4 CLT, 0 simulators), 22 GiB free of 228 (≈50 GB reclaimable: Caches 15, Downloads 10,
  `.npm` 7, an old `portainer-stacks` clone 15), no `node`/`claude`/`codex`; `git`, `rsync`,
  `xcrun` present. jupiter (this machine, x86_64 Linux) has no Swift toolchain. The project
  CLAUDE.md sentence "Merkur IS the local machine — never ssh to it" is wrong (corrected in
  Phase 1). Codex CLI 0.144.6 and `~/.claude/bin/review-gate.py` are available.

**Platform facts (researched 2026-08-23 with dated sources; orchestrator knowledge ends
Jan 2026):** shipping iOS 26.6.1 / Xcode 26.6 (Swift 6.3); iOS 27 + Xcode 27 (Swift 6.4,
Apple-silicon only) in beta, public ~Sept 2026. New Xcode app projects default to Swift **5**
language mode with Approachable Concurrency + MainActor default isolation — a reference app
must opt into Swift 6 mode explicitly. iOS 26 adoption 79 % of all iPhones / 86 % of
last-4-year devices (Apple, 2026-06-07). Apple's own SwiftUI guidance is `@Observable` models
owned by `@State`, injected via `.environment` (no "view model" term); community mainstream is
"MVVM-lite" (one `@Observable` store per feature, no Combine) + **local SwiftPM feature packages
(uncontested)**; TCA is alive (1.26.1) but polarizing → excluded as "common best practice".
`swift-openapi-generator` 1.13.0 (2026-07-06) supports OAS 3.0/3.1, multipart, streaming, raw
bytes; **does not generate security schemes** (auth = client middleware), no cookie params, no
validation keywords. Swift Testing is the default for new test targets since Xcode 16;
`swift format` ships in the toolchain. Xcode Cloud: 25 h/month free. Apple Developer Program
$99/yr. App Review: 2.1(a) demo account required for login apps (a Pi-hole client was rejected
Feb 2026 for reviewer-unreachable backend); 4.8 Sign-in-with-Apple exempt for "your company's
own account setup and sign-in systems"; privacy manifest required since 2024-05-01; unlisted
distribution = full review + request form; TestFlight 100 internal / 10,000 external / 90-day
builds. Share-extension background `URLSession` MUST set `sharedContainerIdentifier`; only one
process may attach to a background session at a time; the ~120 MB extension memory figure is
empirical, not Apple-documented. Precedent: **MoeMemos** (MPL-2.0, App Store, maintained
2026-08-01) authenticates by pasted access token only and passed review for years.

**Scope boundary.**
- IN: bearer auth on all `/api/*` routes; PAT expiry (the expiry half of MEMNG-34); a
  browser-brokered native login that adds **zero** Authelia configuration; an openapi drift test;
  the `memos-ng-ios` repo with timeline/detail/compose/edit/delete/task-toggle, native login,
  share extension + background upload; TestFlight internal; the reference-architecture doctrine
  (draft → accepted); ADR-0015 (memos-ng) and ADR-0001 (memos-ng-ios).
- OUT (explicit deferrals): APNs push (Web Push stays for the PWA); PAT *scopes* (the other half
  of MEMNG-34 — a device token needs full user scope); offline editing/sync (SwiftData cache of
  the timeline is allowed as a read cache only, no conflict resolution); widgets, App Intents,
  watchOS, macOS/Catalyst, iPad-specific layout (iPhone-only listing); custom-URL-scheme callbacks
  (rejected, Decision 2); a public (searchable) App Store listing; any change to the Vue
  SPA beyond regenerating `schema.d.ts`; any change to Authelia or Traefik.
- Phase 8 (demo instance + unlisted App Store) is planned but **user-gated** — it is only
  executed if the user chooses it after Phase 6.

**Hard constraints (settled — do not re-litigate).**
- memos-ng CARDINAL RULE: retag `memos-ng:rollback-<sha>` before overwriting `memos-ng:local`;
  `up -d --force-recreate` never `restart`; migrations expand/contract only, no data loss; this
  plan IS the plan gate for the two prod migrations it contains.
- Authelia is shared household SSO: **no new OIDC client, no new `redirect_uri`, no restart**
  (LSN-0019: a callback change is a two-sided change requiring an IdP restart). The native
  login MUST reuse the existing web callback.
- Token/JWKS via public `https://auth.discomarder.live` only (ADR precedent + CLAUDE.md).
- Local gate (bare, exit codes cited): `uv run pyright app/ && uv run ruff check app/ tests/
  scripts/ && uv run pytest -q && uv run alembic check`. Tests first; no fake tests; never mock
  the unit under test. Every new/changed backend function gets a pytest test.
- `from anyio import to_thread` idiom; no `anyio.to_thread.run_sync`.
- Presigned media caches are bounded to the signature TTL (LSN-0062, memos-ng lesson /link/1664).
- Outgoing text (App Store listing, review notes, any public README): first-person singular, no
  AI attribution, no internal hostnames/ticket keys (`~/.claude/CLAUDE.md` → Outgoing Text).
- Branch discipline: `feature/<slug>` per phase, squash-merge via PR; `docs/plans/` is tracked
  in this repo (8 prior plans committed) — this plan is committed on the Phase 1 branch.

**Terminology.** *PAT* = personal access token (`memos_pat_…`), the only bearer credential.
*Device token* = a PAT created by the native login flow, named after the device. *Web session*
= the `memosng_sid` cookie session. *Native login* = the Phase 2 browser-brokered flow.
*Reference architecture* = the doctrine in `memos-ng-ios` (`CLAUDE.md` + `docs/ARCHITECTURE.md`
+ ADR-0001). *Doctrine v0* = proposed draft (Phase 0); *v1* = accepted (Phase 7).

**Hard-won constraints from prior sessions.** A sanctioned PAT widening already shipped once
(MEMNG-89 widened `GET /api/attachments/{uid}` to `require_pat_or_user`) and the `last_used`
stamp had to be decoupled from the request transaction (MEMNG-81) — reuse `authenticate_pat`'s
throttled stamping, do not add a second stamping path. Worker/app share one image; a deploy is
both containers. Traefik re-syncs a new container in ~20–40 s (brief external 404 is normal).
Codex implementer sandboxes have no Docker/network (cannot run the DB-backed pytest gate) —
backend phases go to an unsandboxed Opus implementer in a worktree; Codex is the reviewer.
A Swift phase cannot be built on jupiter at all — its build/test gate runs on the Mac.

### Decisions most likely to change

1. **Device credential = a PAT presented as `Authorization: Bearer`** (long-lived, revocable,
   optional expiry), not an Authelia-issued JWT validated by memos-ng as a resource server.
   Alternative: public OIDC client in the app with PKCE straight to Authelia + backend JWT
   validation + in-app refresh lifecycle — rejected because it needs a new Authelia client and
   redirect URI (household SSO restart) and a second auth path in the backend. Cost to change
   after build: rewrite of Phase 2 and the Phase 4 login layer; Keychain storage unchanged.
2. **Native callback = an HTTPS callback `https://memos.discomarder.live/ios/auth/callback?code=…`
   consumed by `ASWebAuthenticationSession`'s `.https(host:path:)` callback (iOS 17.4+), backed
   by an `apple-app-site-association` file served by memos-ng and the app's associated-domains
   entitlement.** A custom URL scheme (`memosng://`) was the draft choice and is REJECTED: PKCE
   binds the code to the *initiator's* verifier, so a malicious app that registers the scheme
   can start its own login (Authelia auto-consents an SSO'd user) and receive its own callback →
   a full PAT (T2; plan gate round 1). With an HTTPS callback only the app holding the
   entitlement for the domain receives it. Consequences: the Apple Developer Program (Team ID)
   becomes a Phase 4 dependency; Phase 2 ships the AASA route parameterised by `APPLE_TEAM_ID`
   / `IOS_BUNDLE_ID` (404 until set; settings are read once at process start, so enabling it is
   an env change + same-image `up -d --force-recreate` of `memos-ng-app`, not a code deploy). OPEN (Phase 4 first action): confirm against Apple's
   `ASWebAuthenticationSession.Callback` docs whether the AASA entry is `webcredentials` or
   `applinks` for this callback type. Cost to change: small either way.
3. **Reference architecture = SwiftUI + Observation (`@Observable` store per feature, "MVVM-lite")
   + environment-based DI + local SwiftPM feature packages + `swift-openapi-generator` client +
   Swift Testing + `swift format`, Swift 6 language mode with strict concurrency ON, iOS 26
   minimum, project-as-code via XcodeGen (`project.yml` committed, `.xcodeproj` generated).** Alternatives: TCA (rejected: polarizing, not "common best practice"); Swift 5
   mode (rejected: a reference must be strict from day one); iOS 18 minimum (rejected: iOS 26
   is at ~80 % and iOS 27 ships before the app does). Cost to change: TCA = rewrite Phases 3–5;
   min-iOS raise = trivial, lower = loss of Liquid-Glass-era APIs.
4. **Separate repo `memos-ng-ios`** with a vendored, script-synced copy of memos-ng's
   `openapi.json` (`scripts/sync-openapi.sh` pinning the memos-ng commit SHA). Alternative:
   `ios/` directory in memos-ng. Rejected because a reference project must be cloneable on its
   own. Cost to change: moderate (history move).
5. **`/api/pats/*` management stays web-session-only; a bearer token may only revoke ITSELF**
   (`POST /api/auth/native/logout`). Rationale: a leaked device token must not be able to mint
   successors that survive its own revocation. Alternative: full PAT management over bearer —
   rejected. Cost to change: trivial.
6. **Bearer-first precedence, fail closed**: when a request carries both a bearer header and a
   session cookie, the bearer is authoritative; an invalid bearer is a 401 even with a valid
   cookie. On the session-only routes (`/api/pats/*`) the rule is stricter still: any
   `Authorization: Bearer` header is rejected with 401 before the cookie is consulted, so a
   token can never reach token management even riding alongside a valid cookie. Alternative:
   cookie fallback — rejected (ambiguous credentials should not silently downgrade). Cost to
   change: trivial.
7. **Markdown rendering via the `MarkdownUI` package** (GFM incl. task lists) over a
   hand-written renderer on Apple's `swift-markdown`. OPEN: its 2026 maintenance state was not
   verified — Phase 3's first action checks it; MarkdownUI's self-declared maintenance mode alone does NOT trigger the pivot; the trigger is a
   failed build under Swift 6 strict mode on Xcode 26.6 or missing GFM task-list/table rendering;
   pivot = `swift-markdown` + a small renderer.
8. **Tickets live in windshift MEMNG (id 16) under one Epic labelled `plan` + `ios`**; no new
   workspace until `memos-ng-ios` is cloned by a second project. Cost to change: trivial.

## Goal

Ship a native SwiftUI memos-ng client to the household via TestFlight — timeline, compose/edit,
task toggling, and the one thing the PWA structurally cannot do (share-sheet capture with
background upload) — using an architecture chosen from current (2026) mainstream practice,
reviewed by two independent models, and recorded so that the next iOS project in the estate
starts by cloning `memos-ng-ios` the way new web projects clone memos-ng.

## Approach

**Backend first, minimal and mechanical.** Change `require_user` itself (one function) to accept
a bearer PAT ahead of the cookie, so all 39 per-route sites gain bearer support without a
grep-and-patch; carve `/api/pats/*` out with a new session-only dependency. Add `expires_at` to
PATs (expand-only migration). Make `openapi.json` trustworthy with a drift test, because it
becomes the Swift client's build input. Deploy (Phase 1). Then add the native login as a
*mode* of the existing OIDC transaction: `login?client=ios&code_challenge=…` is remembered in
the encrypted `memosng_oidc_tx` cookie; the unchanged Authelia round-trip lands on the existing
callback, which — for the ios mode — mints a 120-second one-time code instead of a web session
and 302s to the HTTPS callback `https://memos.discomarder.live/ios/auth/callback?code=…` that
only the entitled app can receive; `POST /api/auth/native/exchange {code, code_verifier}`
consumes the code atomically (one `UPDATE … RETURNING`), verifies S256 and returns a device PAT
once. Authelia sees exactly the flow it sees today (Phase 2).

**App as the reference, built in doctrine order.** Phase 3 lays the skeleton that the doctrine
describes — thin app target, `MemosAPI` (generated client + bearer middleware + server config),
`MemosCore` (domain models, `@Observable` stores, pure functions ported from the Vue lib with the
same test cases), `MemosFeatures` (screens), Keychain with app-group access — and proves it with
the MoeMemos-grade paste-a-PAT login and a read-only timeline. Phase 4 adds the native login and
all write paths; Phase 5 the share extension + background upload; Phase 6 the paperwork and
TestFlight. Phase 7 ratifies the doctrine from what actually shipped (v0 → v1), with Codex and
ChatGPT-Pro reviewing the doctrine artifact independently. Phase 8 is the App Store and is the
user's call.

**Review model.** Backend phases: Opus implementer in an isolated worktree, Codex read-only gates
(pre-execution premise gate + post-phase diff gate, `high` reasoning for Phases 1–2 since they
are authz), orchestrator runs the local gate itself and owns git/merge/deploy (`/safe-deploy`).
Swift phases: implementer = a Claude Code session on the Mac; Codex reviews Swift source
read-only (it cannot build iOS); an Opus verifier on the Mac runs `xcodebuild test` and cites
exit codes. Doctrine: Codex + ChatGPT-Pro (`gpt-pro-architect`, review mode), independent,
convergence = high confidence, divergence = orchestrator adjudication recorded in ADR-0001.

## Rejected Alternatives

- **Cross-platform (Flutter / React Native / KMP).** Contradicts the stated goal (native SwiftUI
  reference). Immich's Flutter app is noted as precedent for *why not*: it is not an iOS
  architecture.
- **TCA (The Composable Architecture).** Maintained and capable, but the research brief's own
  sources call it polarizing; a household reference must be the mainstream answer a future
  contributor (or model) recognises without a framework tutorial.
- **Backend as OIDC resource server (app = public OIDC client).** Needs a new Authelia client +
  redirect URI + restart (LSN-0019, household SSO interruption), JWT validation + refresh handling
  in two codebases, and changes the one thing that works. Revisit only if a second native client
  (macOS, watchOS) makes per-device PATs unwieldy.
- **Reusing the web session in a `WKWebView`/cookie jar.** Fights the CSRF middleware, breaks
  background uploads (no cookie in a background `URLSession` from an extension), and is not how
  native clients authenticate.
- **Monorepo `ios/` in memos-ng.** A reference project must be independently cloneable; the
  OpenAPI contract is shared by a pinned vendored copy instead.
- **MoeMemos as the client.** Valuable precedent (token-paste login passed review for years) and
  a 10-minute read-only experiment against the `/api/v1` compat layer, but it targets old memos
  APIs, is read-only against memos-ng, and is not *our* reference architecture.
- **Public App Store listing.** Gains nothing for a household app and creates an obligation to
  support strangers' servers; unlisted distribution exists for exactly this case.
- **Custom URL scheme callback (`memosng://`).** Zero infra, but any app can claim the scheme
  and PKCE does not bind the code to *our* app (see Decision 2). Rejected for v1 and for the
  reference.
- **Doing nothing / PWA only.** The PWA remains for the web; the share-sheet capture and
  background upload are the concrete capability gap (plan 1649 line 97: "Safari can't").

## Phased Breakdown

Each phase is independently shippable and ends in an objective verification gate.

### Phase 0 — Decision records and doctrine draft
- **Branch:** none — BookStack writes plus the `CLAUDE.md` registry block (committed on the
  Phase 1 branch). memos-ng keeps ADRs BookStack-only (verified: no `docs/adr/` exists); do not
  introduce a second home for them.
- **Build:**
  - **ADR-0015 (memos-ng, book 1648 chapter 2690)** "Native iOS client alongside the PWA; device
    credential = PAT bearer; native login brokered through the existing BFF callback". Status
    `proposed` until the user confirms Decisions 1–2, then `accepted`. Must cite: plan prose it
    supersedes (page 1649 §Client Platform Constraints, page 1884), estate ADR-0002 `/link/1429`
    (honoured), bookstack-ng ADR-0002 `/link/1432` (project-scoped, different client type —
    explained, not contradicted), LSN-0019 (why Authelia is untouched).
  - **New BookStack book "memos-ng-ios — Features"** with chapters Decisions + Session Reviews,
    charter page, and the **doctrine v0 page "Reference iOS Architecture (proposed)"**: stack
    table (language mode, min iOS, packages, DI, state, networking/codegen, persistence policy,
    auth/keychain, testing, formatting, CI/TestFlight), module boundaries and dependency rule
    (Features → Core → API; nothing imports Features), concurrency rules (Swift 6 mode,
    `Sendable` at package boundaries, MainActor default isolation in app + Features), error
    and auth-failure doctrine (typed errors; 401 → signed-out state), media cache doctrine
    (`URLCache` bounded by signed-URL TTL, LSN-0062), "how to start a new iOS project from this
    reference" (clone-and-adapt, mirroring the boat-ctl → memos-ng precedent), and an explicit
    **Not in this reference** list (TCA, Combine, singletons for DI, storyboards/UIKit-first).
  - **ADR-0001 (memos-ng-ios) "Reference iOS architecture"**, status `proposed`, recording
    Decision 3 with alternatives; an entry request for the estate ADR index (page 2697)
    allow-list is drafted but applied only at Phase 7 acceptance.
  - Annotate page 1649's client-platform paragraph with a one-line "superseded on the native
    client point by ADR-0015 (/link/<id>)" via `put-safe` — not a rewrite.
- **Do NOT:** write any code; touch Authelia/Traefik docs; mark any ADR `accepted` before the
  user's confirmation; create a windshift workspace for the iOS repo.
- **Verification:** `GET /api/pages/{id}` shows each page with the correct `a:adr`,
  `a:adr-id`, `a:adr-status`, `a:project` tags and root-relative `/link/` hrefs (render check
  per BookStack rules); ADR-0015 text contains the four citations above (grep on the page HTML
  with a positive control); memos-ng `CLAUDE.md` gains a `BookStack Page Registry` block listing
  book 1648, chapters 2689/2690, plan page, ADR-0015 id, and the new book's ids (committed on the
  Phase 1 branch).
- **Status:** pending

### Phase 1 — Bearer auth on every API route, PAT expiry, openapi drift test (memos-ng)
- **Branch:** `feature/bearer-everywhere` (squash-merge to main; carries this plan file + the
  CLAUDE.md registry block)
- **Build:**
  - `app/auth.py`: `require_user` resolves credentials in order (a) `Authorization: Bearer
    memos_pat_…` → `authenticate_pat` (existing hash lookup + throttled `last_used` stamp),
    rejecting expired tokens; (b) else the `memosng_sid` cookie path exactly as today. An invalid
    or expired bearer is a 401 even if a valid cookie is present (Decision 6). Add
    `require_session_user` = the cookie-only behaviour (today's `require_user` body). Keep
    `require_pat_or_user` as a thin alias of `require_user` (remove in a later cleanup, not here).
    Set `request.state.auth_kind = "pat" | "session"` for downstream use.
  - `app/routers/pats.py`: all three routes switch to `require_session_user` (Decision 5), which
    rejects any request carrying an `Authorization: Bearer` header with 401 BEFORE reading the
    cookie (Decision 6). `PatCreate` gains optional `expires_in_days: int | None` (1–365);
    `PatOut`/`PatCreated` gain `expires_at: datetime | None`; `app/services/pats.py:create_pat`
    gains an `expires_at` parameter (default `None`).
  - `app/models.py` + new alembic revision: `pats.expires_at TIMESTAMPTZ NULL` — expand-only,
    no backfill, no default; `alembic check` clean; downgrade drops the column.
  - `app/routers/auth.py`: `POST /api/auth/logout` unchanged; `GET /api/auth/user` now also
    works with a bearer (it inherits from `require_user`; add `auth_kind` to `UserOut`).
  - `/api/events` (`assets.py:811-826`) inherits bearer via its direct `require_user` call —
    no code change expected; a test proves it.
  - `scripts/gen-openapi.py`: reword its comment (the `.github/workflows/ci.yml:76-81` drift step is
    dormant — CI is abandoned — so the local test is the gate); new `tests/test_openapi_drift.py` asserts
    the committed `openapi.json` equals `app.openapi()` dumped with the script's exact
    serialisation (sorted keys, same indent). Regenerate `openapi.json` and
    `frontend/src/api/schema.d.ts` (`npm run gen:api`); `vue-tsc` must stay green; **no other
    frontend change**.
  - `CLAUDE.md`: add the `BookStack Page Registry` block (Phase 0 ids) and correct the host
    topology — merkur is the Mac mini build host (`ssh merkur.local`), jupiter is the local
    machine; delete the sentence "Merkur IS the local machine — never ssh to it".
  - OPEN (implementer decides, documented in the PR): declare `components.securitySchemes.
    bearerAuth` (+ cookie scheme) in the OpenAPI document so the Swift generator's consumers see
    the contract — only if `npm run gen:api` produces a byte-identical `schema.d.ts` before vs
    after (TypeScript types ignore security schemes); otherwise defer to Phase 3 as a Do-NOT.
  - OPEN: verify that structlog request logging never emits the `Authorization` header value;
    if a header dump exists, redact it and add a unit test on the processor.
  - Tests (`tests/conftest.py` gains a `pat_client` fixture: creates a PAT through
    `logged_in_client`, returns an `AsyncClient` with ONLY the bearer header, no cookie jar).
    Required cases, each of which fails if the implementation is removed: bearer on `GET/POST/
    PATCH/DELETE /api/memos…`; `POST /api/uploads` start + `PATCH` chunk + `finalize`;
    `GET/PATCH /api/settings`; `GET /api/export` (or a cheap export route); `GET /api/events`
    first SSE frame; `GET /api/attachments/{uid}/original`; `GET /api/auth/user` returns
    `auth_kind="pat"`; `GET/POST/DELETE /api/pats` with bearer → 401 both without and WITH a valid cookie
    alongside; expired PAT → 401 on
    `/api/memos`; bearer + valid cookie with an INVALID bearer → 401; bearer POST with no
    cookie and no `Origin` header passes CSRF; unknown-prefix `Authorization` header falls
    through to cookie logic (401 without cookie); `expires_in_days` bounds (0 → 422, 366 → 422);
    `test_openapi_drift` goes red when a route's response model changes (prove once in the PR
    by a throwaway diff, cite the red run).
- **Do NOT:** add PAT scopes; change PatManager.vue or any SPA view; touch `compat.py`
  semantics (anonymous → PUBLIC-only stays); touch Authelia/Traefik; add rate limiting; rename
  `require_pat_or_user` call sites.
- **Verification:** local gate bare, all four exit codes cited (pyright 0, ruff 0, pytest 0 with
  count ≥ 760 + new, `alembic check` 0); `docker build` of the image (Dockerfile unchanged, but
  the deploy needs it anyway); Codex post-phase gate at `high` (authz) → APPROVE; merge;
  `/safe-deploy`: retag running image `memos-ng:rollback-<sha>`, build, `up -d --force-recreate`
  (both containers), `alembic upgrade head` by the app on start (existing behaviour; worker gated
  by MEMNG-86), running Image Id == built Id, `/health` 200, prod `alembic_version` == new head.
  **Named acceptance probe on the real surface (dated, in the session review):** with a
  throwaway PAT minted via `docker exec memos-ng-app` against prod (row deleted afterwards) —
  (1) `GET /api/memos` no auth → 401; (2) same with bearer → 200 and a non-zero count;
  (3) `GET /api/auth/user` with bearer → `auth_kind="pat"`; (4) `GET /api/pats` with bearer →
  401; (5) the SPA still works: browser-harness session loads the timeline (cookie path
  regression). Known-unknown: if the throttled `last_used` stamp path cannot run inside the
  request for bearer on SSE, pivot = stamp skipped for `/api/events` only, documented.
- **Status:** pending

### Phase 2 — Browser-brokered native device login (memos-ng)
- **Branch:** `feature/native-login`
- **Build:**
  - `GET /api/auth/login` accepts `client=ios` + `code_challenge` (43–128 chars, base64url
    alphabet, S256 only) + `device` (≤ 64 chars, display name); stored in the encrypted
    `memosng_oidc_tx` cookie alongside `{state, verifier}`; `next` is ignored in ios mode. The
    Authelia redirect is byte-for-byte the one used today (same client_id, same `redirect_uri`,
    same scopes).
  - `GET /api/auth/callback`: after the existing code exchange + id_token validation + user
    upsert, **if `client == "ios"`**: do not create a web session and do not set `memosng_sid`;
    insert a `native_auth_codes` row `{code_hash, user_id, code_challenge, device_name,
    expires_at = now+120 s, consumed_at NULL}` (new table, expand-only migration); 302 to
    `settings.NATIVE_CALLBACK_URL` (default `https://memos.discomarder.live/ios/auth/callback`)
    with `?code=<random 32 B urlsafe>`. On an IdP error (`error=` param) in ios mode: 302 to the
    same URL with `?error=<code>`. Opportunistically delete rows whose `expires_at < now() −
    1 hour` on each mint (no worker job) — NOT merely expired rows, so a code stays classifiable
    as `code_expired` for an hour after expiry; after cleanup it reports `code_invalid`
    (documented; both are terminal for the client).
  - `POST /api/auth/native/exchange` body `{code, code_verifier}` (no cookie, no CSRF by
    construction): **consume atomically in ONE statement** — `UPDATE native_auth_codes SET
    consumed_at = now() WHERE code_hash = :h AND consumed_at IS NULL AND expires_at > now()
    RETURNING user_id, code_challenge, device_name` — then verify `S256(code_verifier) ==
    code_challenge` (a mismatch leaves the code consumed: one attempt per code); create a PAT
    named `device_name` via `services.pats.create_pat` with `expires_at = now + NATIVE_TOKEN_DAYS`
    (default **365**); return `{token, expires_at, user}` — the one and only plaintext exposure.
    Failures → 400 with a stable `detail` code (`code_invalid|code_expired|code_consumed|
    verifier_mismatch`), nothing else leaked (`expired` vs `consumed` derived from a follow-up
    read when the UPDATE matched nothing; `invalid` when no row exists — including an expired
    code already swept by the 1-hour cleanup). On `verifier_mismatch` the consumption is
    COMMITTED before the 400 is raised (the request-scoped session does not auto-commit).
    Table shape: integer PK, `code_hash` UNIQUE + indexed, `user_id` FK → users ON DELETE
    CASCADE, timestamps TIMESTAMPTZ.
  - `POST /api/auth/native/logout`: requires a bearer (`auth_kind == "pat"`), deletes THAT PAT
    row only (Decision 5); 401 otherwise.
  - `GET /.well-known/apple-app-site-association`: anonymous, `application/json`, built from
    `APPLE_TEAM_ID` + `IOS_BUNDLE_ID` (404 while either is unset); emits `webcredentials` and/or
    `applinks` per a settings switch `AASA_KEYS`, so Phase 4 enables it with an env change plus a
    same-image `up -d --force-recreate` of `memos-ng-app` (no build, no migration) — settings
    are instantiated once at startup (`app/config.py`), a bare env edit changes nothing. `GET /ios/auth/callback`: anonymous minimal HTML "open the memos-ng app" page for the
    not-installed case (with the app present the authentication session intercepts the URL).
  - Config: `NATIVE_CALLBACK_URL`, `NATIVE_CODE_TTL_S = 120`, `NATIVE_TOKEN_DAYS = 365`,
    `APPLE_TEAM_ID: str | None = None`, `IOS_BUNDLE_ID = "live.discomarder.memosng"`,
    `AASA_KEYS = ["webcredentials"]`.
  - Regenerate `openapi.json` + `frontend/src/api/schema.d.ts` (the Phase 1 drift test enforces
    it); `vue-tsc` green; no SPA view change.
  - Tests (through the mocked-OIDC flow the `logged_in_client` fixture already uses, plus a CLEAN
    mocked-OIDC client with no session for the no-session assertions): ios-mode login 302
    targets the SAME Authelia authorize request as web mode — parse both `Location` URLs and
    assert identical scheme/host/path, an identical SET of query keys, and identical values for
    every key except the per-request randoms `state`, `code_challenge` (the BFF's own PKCE
    challenge; the app's challenge is never forwarded) and `nonce` if present — the "Authelia
    untouched" proof in code; the app-supplied `code_challenge` and `device` MUST NOT appear in
    the authorize URL; callback in ios mode → 302 to
    `NATIVE_CALLBACK_URL?code=` and NO `memosng_sid` cookie and NO `sessions` row; exchange happy
    path returns a PAT that then authenticates `GET /api/memos` and carries `expires_at ≈
    now+365 d`; wrong verifier → 400 `verifier_mismatch` and the code is consumed; second
    exchange → 400 `code_consumed`; expired (frozen clock) → 400 `code_expired`; unknown code →
    400 `code_invalid`; **two concurrent exchanges (`asyncio.gather`) of one code → exactly one
    200 and one 400 `code_consumed`**; IdP error → 302 with `error=`; `native/logout` revokes only
    the presenting PAT (two PATs, one survives); web-mode login/callback regression unchanged;
    `code_challenge` validation (too short / bad alphabet → 422); `device` propagates verbatim
    into the created PAT's name and `next` is ignored in ios mode; expired-code test backdates
    `expires_at` in PostgreSQL (not only a frozen Python clock); **route precedence over the SPA
    catch-all** (`app/main.py` SPA fallback): `GET /ios/auth/callback` returns 200 `text/html`
    containing the marker `data-memosng-native-callback` (NOT the SPA shell — assert the SPA's
    root marker is absent), `GET /.well-known/apple-app-site-association` returns 404 when unset
    and `application/json` with the team + bundle when set (never SPA HTML); the OpenAPI
    security description marks `native/exchange` as anonymous.
- **Do NOT:** add a second Authelia client; add a custom URL scheme; build any UI; add refresh
  tokens; store OIDC tokens for device logins (the PAT is the credential; the OIDC tokens from
  the exchange are discarded); change web-mode behaviour; add rate limiting (codes are 256-bit
  random, single-use, 120 s).
- **Verification:** local gate bare (4 exit codes); Codex pre-execution premise gate (high) on
  this spec against the post-Phase-1 code BEFORE implementation; Codex post-phase gate (high);
  `/safe-deploy` as in Phase 1 (rollback retag, migration to the new head, Image Id, `/health`).
  Acceptance probe: a real browser (browser-harness or the user's phone) opens
  `https://memos.discomarder.live/api/auth/login?client=ios&code_challenge=<known>&device=probe`,
  completes Authelia, and the harness captures the 302
  `Location: https://memos.discomarder.live/ios/auth/callback?code=…` (no app needed yet);
  `curl -X POST …/api/auth/native/exchange` with the matching verifier returns a token that lists
  memos; a second identical POST → 400 `code_consumed`; `native/logout` with the token → the
  `pats` row is gone (DB read-back); `curl -s -o body.html -w '%{http_code} %{content_type}'
  https://memos.discomarder.live/ios/auth/callback` → `200 text/html…` and `grep -c
  data-memosng-native-callback body.html` → 1 (GET, not HEAD — a GET-only route answers 405 to
  HEAD), and `curl -s -o /dev/null -w '%{http_code}' …/.well-known/apple-app-site-association` →
  404 (unset in prod until Phase 4) — neither returns the SPA shell. `diff` of the live Authelia
  config hash before/after the
  phase is empty (positive control: the hash changes when a byte is appended to a scratch copy).
- **Status:** pending

### Phase 3 — `memos-ng-ios` foundation: the reference skeleton
- **Branch:** TWO declared branches. (3a) memos-ng `feature/markdown-parity-fixture` — a small
  PR merged FIRST: (i) `fixtures/markdown-parity.json` = a **versioned** document
  `{schema: 1, generator: "<renderer sha>", cases: [{id, markdown, projection}]}` that CARRIES
  each case's Markdown input; (ii) `frontend/scripts/gen-markdown-parity.ts` run by
  `npm run gen:markdown-parity` — the ONLY writer, rendering each case through the real SPA
  renderer (`renderMemoMarkdown` incl. DOMPurify) under **jsdom** (DOMPurify supports jsdom,
  not happy-dom — per-file `@vitest-environment jsdom`, jsdom added as a dev dependency if
  absent); (iii) `frontend/src/lib/__tests__/markdown-parity.spec.ts` — READ-ONLY drift test
  that re-renders the committed inputs and fails on any projection difference (it never
  writes); (iv) `fixtures/markdown-parity.schema.md` defining the projection exactly: an ordered
  list of blocks `{type: heading|paragraph|list|task|code|blockquote|table|hr, level?, text,
  checked?, items?, rows?, links, inline}` where `inline` is the ordered list of styled spans
  `{type: strike|em|strong|code, text}` found in the block (so a renderer that drops
  strikethrough or emphasis cannot pass), and `text` is Unicode NFC, runs of whitespace collapsed to
  one space, trimmed, `<br>` (soft breaks) kept as `\n`; code blocks verbatim; tables as rows
  of cell texts; links as ordered `{href, text}` with only `http(s)`/`mailto` hrefs retained
  (others projected as plain text); tag-only lines absent. Verification for 3a: local gate
  + `npm run typecheck` + `npm test` green; the drift spec shown red once against a hand-edited
  projection; Codex diff review (medium). (3b) new repo
  `memos-ng-ios` (GitHub, private until Phase 7), default branch `main`, work on
  `feature/foundation`, which vendors the merged fixture by memos-ng SHA.
- **Execution model:** implementer = Opus `implementer` in a worktree on jupiter (Swift is text;
  no toolchain is needed to write it); build host = **merkur** once Xcode 26.6 is installed
  (user action: ~30 GB disk cleanup from the reclaimable set + App Store install; then
  `xcode-select -s`, license acceptance and the iOS 26 simulator runtime are scriptable over
  SSH). The gate is **`bin/mac-gate.sh`** — the first deliverable of this phase. Contract: (1) refuse
  (exit 2) unless the local tree has a HEAD; (2) compute the remote target
  `~/build/memos-ng-ios/gate/<branch>/` and refuse unless its realpath is inside
  `~/build/memos-ng-ios/gate/` (never `~/projects/`); (3) `rsync -a --delete` the worktree
  EXCLUDING `.git`, `.build`, `DerivedData`, `*.xcodeproj`, `*.xcresult`; (4) on merkur, with
  `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer` exported, run in order and
  **fail-fast**: `xcodegen generate` → `swift format lint --strict --recursive .` →
  `xcodebuild test -scheme MemosNG -destination 'platform=iOS Simulator,name=<iPhone>'` → for each
  package `swift package show-dependencies` (dependency-rule check) → a test-count check that
  reads per-bundle test counts from the `.xcresult` (`xcrun xcresulttool get test-results
  summary`/`tests` JSON — Swift Testing reports `Test run with N tests`, NOT XCTest's
  `Executed N tests`, which may read 0 for a correct Swift Testing bundle) and fails if any of the
  three suites ran 0 tests (the empty-project first probe is expected to fail here — that failure
  IS the proof the check engages); (5) the
  gate log begins with provenance — local HEAD SHA, the FULL `git status --porcelain` (not only
  `--stat`) and `git diff --stat` — and ends with one summary line listing every step's exit
  code; (6) the script's own exit code is the first non-zero step (never masked by a pipe) and
  is returned to the caller; (7) the `.xcresult` and simulator screenshots are copied back to
  the local scratchpad. **Exchange contract (the B7 guard):** git is the ONLY medium between
  machines; the gate directory is never a git checkout and never opened in Xcode; the user's
  hands-on checkout is an ordinary clone at `merkur:~/projects/memos-ng-ios` (run `xcodegen
  generate` after clone) that is never an rsync target; implementers `git pull` before starting.
- **Build:**
  - Xcode project `MemosNG` **generated by XcodeGen from a committed `project.yml`** (project as
    code: the `.xcodeproj` is gitignored and regenerated by `bin/mac-gate.sh` and by a developer's
    `xcodegen generate` after clone — a hand-maintained `.xcodeproj` is an opaque, merge-hostile
    artifact that agents on jupiter cannot author or review; XcodeGen 2.46 is installed on merkur),
    bundle id `live.discomarder.memosng`, iOS 26.0 minimum, iPhone only; `project.yml` declares
    one scheme `MemosNG` whose test action runs the package test targets `MemosCoreTests`,
    `MemosAPITests` and `MemosFeaturesTests` — `xcodebuild test` MUST execute all three, **Swift 6 language mode + strict concurrency enabled explicitly** in every
    target and every local package (`swiftLanguageModes: [.v6]`), Approachable Concurrency +
    MainActor default isolation on the app and Features targets. **Build with Xcode 26.6, not
    the 27 beta** (the Liquid Glass compatibility key is ignored under 27; keep the toolchain
    stable until Phase 6).
  - Local SwiftPM packages under `Packages/`: `MemosAPI` (swift-openapi-generator plugin +
    `openapi.json` AND `fixtures/markdown-parity.json` vendored together by
    `scripts/sync-openapi.sh <memos-ng-full-sha>` (both fetched from that one SHA via `git show`;
    SHA recorded in `Packages/MemosAPI/OPENAPI_SOURCE`); `BearerAuthMiddleware`; `ServerConfiguration` (base URL);
    `APIClientFactory`), `MemosCore` (domain models mapped from generated types; `@Observable`
    stores `SessionStore`, `TimelineStore`; pure functions `stripTagOnlyLines` / `isTagOnlyLine`
    ported from `frontend/src/lib/markdown.ts:24-42` and `toggleTask` from
    `frontend/src/lib/tasks.ts`, each with the SAME vectors as `frontend/src/lib/__tests__/`;
    `Keychain` wrapper using `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` in the app's
    DEFAULT keychain — it takes an optional access-group parameter that stays unset until Phase 4
    provisions the App Groups + Keychain Sharing entitlements (Phase 5's extension consumes them) (setting `kSecAttrAccessGroup` without the
    entitlement fails with `errSecMissingEntitlement`)), `MemosFeatures` (`TimelineView`,
    `MemoDetailView`, `SettingsView`/`SignInView`). Dependency rule enforced by package
    manifests: Features → Core → API; the app target only composes. Third-party: `MarkdownUI`
    (Decision 7, verified first), nothing else.
  - **Markdown rendering contract** (mirrors `frontend/src/lib/markdown.ts:10-58`): raw HTML
    renders as literal text (`html: false`); a single newline is a line break (`breaks: true`);
    bare URLs become links (`linkify`); GFM task lists, tables, strikethrough, headings, block
    quotes, code; a link is tappable only when its scheme is `http`, `https` or `mailto`
    (`ALLOWED_URI_REGEXP`), any other scheme renders as plain text; tag-only lines are stripped.
    **Parity fixture (consumer side):** the Swift side vendors the merged
    `fixtures/markdown-parity.json` next to `openapi.json` (same source SHA) and a Swift Testing
    suite renders each case's **carried Markdown input** through the app's renderer and asserts
    the same block-structured projection (schema 1) — headings, lists, task states, code,
    blockquotes, tables, strikethrough, links, soft breaks, stripped tag-only lines. A renderer
    that flattens structure or tests different inputs cannot pass. Residual: jsdom ≠ Safari;
    accepted and documented in the schema file.
  - Sign-in v1: server URL + pasted PAT (MoeMemos pattern) → validated with `GET /api/auth/user`
    → Keychain. Signed-out state on any 401.
  - Timeline: cursor-paginated `GET /api/memos`, pull-to-refresh, `AsyncImage` for `thumb`/
    `mobile` tiers with a `URLCache` whose entries cannot outlive the signed URL's `exp` (LSN-0062;
    rely on the server's `Cache-Control: private, max-age` and assert it in a Core test that
    parses `exp` from the URL). Memo detail renders markdown with task checkboxes read-only.
  - Tooling: `.swift-format` config + `bin/mac-gate.sh`; Swift Testing targets for Core and API
    (middleware injects the header; store logic against a fake `APIProtocol` — the generated
    protocol is the mock seam; never mock the store); `CLAUDE.md` carrying the doctrine's
    executable rules (gate command, package rule, concurrency rule, test rule, outgoing-text
    rule); `docs/ARCHITECTURE.md` = doctrine v0 mirrored from the BookStack page; README with the
    sync-openapi and parity-fixture procedures.
  - **First actions (premise probes, LSN-0040):** (1) run `bin/mac-gate.sh` on the empty project
    to prove the rsync → remote build → exit-code path AND the refusal paths (target inside
    `~/projects/`, no HEAD) before any Swift is written; (2) run the
    generator against the vendored `openapi.json` and enumerate every warning/unsupported
    feature against the official feature table — pivot: fix memos-ng's schema output in a small
    PR rather than hand-writing the client; (3) verify `MarkdownUI` maintenance (Decision 7
    pivot to `swift-markdown` + a small renderer).
- **Do NOT:** add TCA/Combine/any DI container; add SwiftData (a read cache is Phase 4's call);
  add a custom URL scheme; implement writes, share extension, push, widgets; target iPad; add
  SwiftLint (swift-format only in v1); commit a token, server URL, or team id.
- **Verification:** branch 3a merged with its vitest drift test shown green and red once;
  `bin/mac-gate.sh` → 0 with each inner check's exit code shown (`xcodebuild
  test -scheme MemosNG -destination 'platform=iOS Simulator,name=iPhone 17'`, `swift format lint
  --strict --recursive .`); Core/API packages build in Swift 6 mode with `-warnings-as-errors`;
  the generated client round-trips `GET /api/memos` against production with a real PAT on a
  merkur simulator (screenshot copied back + the request visible in the memos-ng access log,
  dated); the dependency rule is checked mechanically — `grep -n "MemosCore\|MemosFeatures"
  Packages/MemosAPI/Package.swift` → 0 hits and `grep -n "MemosFeatures"
  Packages/MemosCore/Package.swift` → 0 hits, with `grep -n "MemosCore"
  Packages/MemosFeatures/Package.swift` ≥ 1 as the positive control; parity suite green with
  ≥ 15 cases and shown red once against a deliberately altered projection; the gate log shows
  the `.xcresult`-derived count N ≥ 1 for each of the three suites; Codex read-only
  review of the Swift diff (medium) → APPROVE; an Opus verifier re-runs `bin/mac-gate.sh`.
  Known-unknown: simulator reachability of `memos.discomarder.live` from merkur is assumed;
  pivot = run the backend on merkur (`uv run` + `compose.dev.yaml`) for development and keep prod
  for acceptance.
- **Status:** pending — blocked on Xcode 26.6 on merkur (user action: disk cleanup + install)

### Phase 4 — Native login and all write paths (memos-ng-ios)
- **Branch:** TWO declared branches (premise gate 01a03201, round 1). (4a) memos-ng
  `fix/nullable-request-schemas`, merged FIRST: the four request-side nullable-enum fields
  (`MemoCreate.visibility`, `MemoPatch.visibility`, `SettingsPatch.default_visibility`, and the
  `visibility` query params of list/export) switch to `SkipJsonSchema[None]` so the schema emits
  a plain `$ref` with the field simply not required — no API behaviour change (absent ≡ null for
  these fields), `openapi.json`/`schema.d.ts` regenerated, drift test green, a pytest asserting
  none of the four carries an `anyOf` with a null branch; partial delivery of MEMNG-105.
  (4b) memos-ng-ios `feature/native-login-writes`, vendoring 4a's merge SHA.
- **Dependencies:** Phase 2 deployed; Phase 3 merged; **Apple Developer Program enrolled** (the
  Team ID is needed for the associated-domains entitlement and the AASA file — user action,
  $99/yr, moved here from Phase 6).
- **Build:** first action — confirm against Apple's `ASWebAuthenticationSession.Callback.https(
  host:path:)` documentation which AASA key (`webcredentials` / `applinks`) this callback
  requires, set memos-ng's `APPLE_TEAM_ID` / `AASA_KEYS` in the stack env and apply with a same-image
  `docker compose up -d --force-recreate memos-ng-app` (a config reload under the CARDINAL RULE:
  same Image Id before/after, `/health` 200 — no build, no migration), then verify `https://memos.discomarder.live/.well-known/apple-app-site-association` serves JSON
  with the Team ID + bundle id (curl, dated). Then: `ASWebAuthenticationSession` with the
  `.https(host: "memos.discomarder.live", path: "/ios/auth/callback")` callback,
  `prefersEphemeralWebBrowserSession = false` (Authelia's browser session is reused); PKCE
  verifier via `CryptoKit` (32 random bytes → base64url; challenge = S256); exchange → Keychain;
  sign-out = `POST /api/auth/native/logout`, wiping the Keychain ONLY on 2xx or 401 (401 = the
  token is already dead); on a transport error or 5xx the credentials are KEPT, the failure is
  surfaced, and the user gets an explicit "sign out without revoking" choice — silently wiping
  after a failed revoke leaves a stolen PAT valid for up to a year while the UI claims success
  (premise gate B3). The Keychain switch to the app-group access group ships with a dual-read
  migration: read the group namespace first, fall back to the legacy nil-group items and migrate
  them on first read, with tests for both paths (premise gate B2). Entitlements are generated
  through `project.yml` WITHOUT any committed team id, and the teamless `bin/mac-gate.sh` run
  must stay green. `?mode=developer` is an experiment, not a guaranteed simulator pivot. Tags
  are content-derived hashtags — there is no tags request field. The native-login URL builder
  (client=ios, code_challenge, bounded nonempty device name) is a pure tested function; session
  error paths (presentation failure, user cancel, backend `?error=` callback, malformed
  callback, PKCE-secret disposal) are all handled and tested.
  Re-sync the vendored `openapi.json` to the post-Phase-2 memos-ng SHA (`scripts/sync-openapi.sh`)
  so the generated client carries the native-auth operations. Compose, edit, delete, pin,
  visibility, tags; task toggle = `toggleTask` → `PATCH /api/memos/{uid}` with optimistic store
  update and rollback on failure. Error doctrine: typed `APIError`, `.unauthorized` →
  `SessionStore.signOut()`. Pasted-PAT sign-in stays as the "advanced" option (reviewer/demo
  path). Optional `SwiftData` read cache of the timeline (no offline writes). Entitlements (Phase 4 provisions ALL three; Phase 5 only adds the extension target to the
  group): Associated Domains (`webcredentials:memos.discomarder.live` or `applinks:` per the
  probe), App Groups (`group.live.discomarder.memosng`), Keychain Sharing — and the `Keychain`
  wrapper's access group is set to the app group here.
- **Do NOT:** custom URL scheme fallback; offline edit queue; conflict resolution; share
  extension; iPad layout.
- **Verification:** Swift Testing: PKCE known-answer vectors (RFC 7636 Appendix B), `toggleTask`
  vectors identical to `tasks.spec.ts`, optimistic rollback on a failing fake API, 401 →
  signed-out transition; `bin/mac-gate.sh` → 0; end-to-end on a **physical device** (associated
  domains need extra setup on a simulator — known-unknown; pivot: AASA developer mode /
  `?mode=developer` for the simulator): native login against production completes and a created
  memo appears in the SPA (browser-harness read-back) and in the DB; Codex read-only diff review
  (medium) → APPROVE.
- **Status:** pending

### Phase 5 — Share extension + background chunked upload (memos-ng-ios)
- **Branch:** `feature/share-upload`
- **Build:** `MemosShare` extension target + `MemosUpload` package (Core-level): accepts image/
  video/text/URL from the share sheet, writes the payload file-based into the app-group
  container, enqueues an `UploadJob` (persisted JSON in the shared container), and drives
  `POST /api/uploads` → `PATCH` chunks (5 MiB, `Upload-Offset`) → `finalize` → `POST /api/memos`
  through a background `URLSession` whose configuration sets `sharedContainerIdentifier`; the
  extension hands off and the app adopts the session on next launch (single-attach rule);
  `HEAD` offset probe on resume. Memory: never load media into memory in the extension; file
  upload tasks only. Keychain shared via the app-group access group.
- **Do NOT:** transcode/resize on device (the worker owns tiers; HEIC/HEVC originals are the
  archive master per ADR-0008); background *downloads*; multiple parallel uploads in v1; push.
- **Verification:** Swift Testing for the upload state machine (chunk math on 0/1/5/5.5/1024 MiB
  sizes, resume from a `HEAD` offset, finalize only after the last chunk, error → retry budget);
  `xcodebuild test` 0; device acceptance: share a photo from Photos.app, background the app
  immediately, and verify the `uploads`/`attachments` rows and a `thumb` rendition on prod via
  DB read-back within the worker's normal latency; Saturn host temperature stays < 70 °C during
  the test (thermal coupling rule); Codex diff review (medium) → APPROVE.
- **Status:** pending

### Phase 6 — App Store Connect, Xcode Cloud → TestFlight internal
- **Branch:** `feature/release-pipeline` (repo config only)
- **Build:** App ID capabilities (Associated Domains, App Groups, Keychain
  Sharing — configured in Phase 4) confirmed in App Store Connect; `PrivacyInfo.xcprivacy` declaring the required-reason APIs the
  app uses (UserDefaults, file timestamps); `ITSAppUsesNonExemptEncryption = NO`; app icon;
  a privacy-policy page (static, served where the household already hosts static pages — OPEN:
  vserver vs a memos-ng route; decide in-phase, never in the SPA bundle); Xcode Cloud workflow
  building `main` → TestFlight internal group "household"; build number from CI; version 0.1.0.
  Listing text drafted in first-person singular with no internal hostnames (outgoing-text gate
  grep with positive control) but NOT submitted.
- **Do NOT:** external TestFlight; App Store submission; unlisted request (Phase 8); Fastlane
  (Xcode Cloud's free tier covers this; revisit only if the 25 h/month cap is hit).
- **Verification:** a TestFlight build installs on two household iPhones; each performs native
  login, creates a memo, and shares a photo that lands in prod (DB read-back, dated); crash-free
  in Xcode Organizer for 7 days; `git status` clean in both repos; no secrets in the repo
  (grep for `memos_pat_`, team id, with positive control).
- **Status:** pending

### Phase 7 — Ratify the reference architecture (doctrine v1)
- **Branch:** `feature/doctrine-v1` (memos-ng-ios docs) + BookStack
- **Build:** revise doctrine v0 into v1 from what actually shipped (every deviation from v0 is
  listed with its reason); ADR-0001 (memos-ng-ios) → `accepted`; estate ADR index (page 2697)
  allow-list entry via `put-safe`; `docs/ARCHITECTURE.md` + `CLAUDE.md` final; "Start a new iOS
  project from this reference" checklist (rename bundle id/app group/scheme, swap `openapi.json`,
  delete Features, keep Core/API shape). Reviews: Codex (`max`, read-only against the repo) and
  ChatGPT-Pro (`gpt-pro-architect`, review mode, NO repo access, given the doctrine page only),
  launched independently and not primed with each other's findings; the orchestrator adjudicates
  convergence/divergence in the ADR's "Review record" section. ADR-0015 (memos-ng) gets a
  "delivered by" pointer to the TestFlight build.
- **Do NOT:** rewrite the app to satisfy a reviewer's taste — only BLOCKING findings (a rule the
  app violates, a contradiction between doctrine and code) cause a code change; advisories ship
  as backlog tickets.
- **Verification:** both review verdicts recorded verbatim in the ADR; doctrine page tags
  `a:adr-status=accepted`; estate index row renders as a root-relative link (GET html check);
  a docs-accuracy pass (`docs-accuracy-auditor`) finds zero numeric/behavioural claims in the
  doctrine that the repo contradicts.
- **Status:** pending

### Phase 8 — Demo instance + unlisted App Store submission (USER-GATED)
- **Branch:** `feature/demo-stack` in `/opt/jupiter-stacks` (pins-to-main repo, needs
  `ALLOW_MAIN_COMMIT=1` per convention) + `feature/app-review` in memos-ng-ios
- **Build:** a second memos-ng stack (`memos-ng-demo`, own Postgres on jupiter, seeded with
  synthetic memos/photos, separate Traefik router `demo-memos.discomarder.live`) whose sign-in
  for the reviewer is a pasted PAT supplied in App Review notes (the 2.1(a) requirement met
  without exposing Authelia); review notes stating the 4.8 exemption ("the app exclusively
  uses my own server's account system") and the demo credentials; screenshots for the required
  iPhone sizes; unlisted-distribution request form submitted AFTER the build is approved for
  final distribution.
- **Do NOT:** add a demo account to the production Authelia; reuse prod data; a public listing.
- **Verification:** the demo URL serves `/health` 200 and the seeded timeline over bearer from
  outside the LAN; App Review approval recorded; the unlisted link installs on a non-tester
  device.
- **Status:** pending — the go/no-go is asked after Phase 6; execution only after Phase 7 and
  only on explicit user choice

## Execution Protocol (Agentic Review Loop)

Per phase, three gates in order:

1. **Pre-execution gate (before any code):** Codex reviews the phase's Build/Do-NOT/Verification
   spec against the current branch state. Write the prompt to a file, then
   `codex exec --sandbox read-only -C <repo> -c model_reasoning_effort=high - < prompt.txt`
   (background, closed stdin). The prompt lists every file the phase touches with line anchors
   (for Phase 1: `app/auth.py:126-199,321-358`, `app/routers/auth.py:34-105`,
   `app/routers/pats.py`, `app/models.py:359-367`, `app/csrf.py:100-106`,
   `app/config.py:143-189`, `app/routers/assets.py:487-531,811-827`,
   `app/routers/uploads.py:61-126`, `app/routers/compat.py:40-46`, `tests/conftest.py:99-131`,
   `scripts/gen-openapi.py`, `app/services/pats.py`, `.github/workflows/ci.yml:76-81`; for
   Phase 3: `frontend/src/lib/markdown.ts`, `frontend/src/lib/tasks.ts`,
   `frontend/src/lib/__tests__/`), demands independent analysis FIRST, then the spec, then a typed
   verdict (`VERDICT: APPROVE | REVISE` + file:line evidence) in a fenced block appearing exactly
   once at the end; parse the LAST occurrence. Severity bar: BLOCKING (correctness / security /
   data-loss / public contract / plan-fidelity / fake test, each with file:line + a concrete
   failure) vs ADVISORY; the reviewer steelmans each blocker against itself; "a clean APPROVE
   with 3 advisories beats a REVISE over polish". DO-NOT-REPORT: style/lint, defensive coding
   absent a spec requirement, speculative issues without a failure path, "could also test"
   beyond the phase contract. Threat model for Phases 1–2 is stated in the prompt: a leaked or
   stolen PAT, a malicious app on the same device registering the `memosng` scheme, and a
   cross-site browser request — findings outside it are advisory. Fold REVISE points in before
   implementing; delta rounds via `codex exec --sandbox read-only resume <session-id> - <
   delta.txt` (global flags before `resume`).
2. **Implementation:** Opus `implementer` in an isolated worktree on jupiter for every phase —
   Swift source needs no toolchain to write; Swift phases run their gate through
   `bin/mac-gate.sh` (rsync to merkur, remote `xcodebuild test` + `swift format lint`, remote
   exit code returned) and the user's hands-on Xcode work happens in a separate git clone on merkur, exchanged
   only through commits (never rsynced over) —
   honoring the phase's Do-NOT cuts; the orchestrator owns git
   (branch pre-created, commits from the implementer's manifest), re-runs the verification
   commands itself bare and cites exit codes. If reality contradicts the phase spec
   mid-implementation, stop — state what changed, update this plan, re-run the pre-execution
   gate if material.
3. **Post-phase code gate:** Codex reviews the actual diff read-only:
   `~/.claude/bin/agent-review-orchestrator.py` + `review-gate.py` (spec
   `~/.claude/docs/agent-review-gate.md`) with gate commands run by the orchestrator and real
   exit codes recorded; fallback `codex exec --sandbox read-only -C <repo>` with `git diff
   main...HEAD`. Effort: high for Phases 1–2 (authz), medium for Phases 3–6 diffs, max for the
   Phase 7 whole-repo audit. A second instance of any leak class → the implementer enumerates
   every site of the class with dispositions; the next round verifies the enumeration.
   Delta rounds check only that blockers are closed; an advisory-only round means ACCEPT.

Before each merge that deploys (Phases 1, 2): a **final cross-model audit** of the branch against
this plan's Acceptance Criteria items 1–3, item by item, with evidence, plus the standard
release gates (`/preflight` before the deploy session, `/safe-deploy` for the deploy, untracked
files hook, `git status` clean). If Codex is unavailable, substitute a fresh Claude subagent in
an isolated worktree with the same adversarial prompt — never skip a gate. Phase 7's doctrine
review adds the independent ChatGPT-Pro round described in that phase.

## Dependencies

- Phase 0 needs the user's confirmation of Decisions 1–3 (ADR status `accepted`).
- Phase 1 needs nothing beyond main at `b7f3b68`.
- Phase 2 needs Phase 1 deployed (it issues PATs and relies on bearer everywhere).
- Phase 3 needs Phase 1 deployed (bearer on `/api/memos`), its own memos-ng branch 3a (parity
  fixture) merged first, **Xcode 26.6 on merkur** (user action: disk cleanup + install), and
  `bin/mac-gate.sh` proven on an empty project.
- Phase 4 needs Phases 2 and 3 and the **Apple Developer Program** (Team ID for the
  associated-domains entitlement + AASA). Phase 5 needs Phase 4. Phase 6 needs Phase 5. Phase 7
  needs Phase 6 (doctrine ratified from a shipped app). Phase 8 needs Phase 7 and an explicit
  user go.

## Dependencies on Current State

- Prod at alembic head `8b7d6acb2567` == repo head; image `memos-ng:local` = `7616161a…`
  (v1.4.0) running since 2026-08-19 04:37 UTC (observed 2026-08-23).
- `pats` has 2 rows, `sessions` 35, `users` 2, `memos` 231 (2026-08-23) — the expiry column
  migration touches 2 rows, all of which stay non-expiring (NULL).
- `tests/conftest.py` `logged_in_client` mocks `exchange_code`/`validate_id_token`; the Phase 2
  tests extend this fixture pattern rather than adding `dependency_overrides`.
- `frontend/src/components/PatManager.vue` + spec exist; `npm run gen:api` regenerates
  `schema.d.ts`; `vue-tsc` is part of the frontend check.
- windshift workspace MEMNG id 16, labels `plan` (65) and `security` (6) exist; `ios` does not.
  MEMNG-34 (PAT expiry + scoping) is Open and is partially delivered by Phase 1.
- BookStack: book 1648, chapter 2689 (Session Reviews), chapter 2690 (Decisions), plan pages
  1649/1884, estate ADR index 2697; no `BookStack Page Registry` block in CLAUDE.md yet.
- Codex 0.144.6 at `~/.npm-global/bin/codex`; review-gate scripts present.
- merkur: Mac mini M1, 16 GB, macOS 26.6, no Xcode (CLT only), 22 GiB free, `rsync`/`git`/`xcrun`
  present, no `node`/`claude`/`codex` (probed 2026-08-23). jupiter has no Swift toolchain.
- `.github/workflows/ci.yml:76-81` contains an OpenAPI drift step that never runs (CI abandoned).
- `app/services/pats.py:create_pat(db, user, name)` is the single PAT creation path.
- `frontend/src/lib/tasks.ts` + `frontend/src/lib/__tests__/tasks.spec.ts` hold the task-toggle
  logic and its vectors; `frontend/src/lib/markdown.ts:10-22,53-57` hold the renderer options and
  the DOMPurify allowlist (`ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i`).

## Risks

- **Bearer on every mutating route widens what a leaked PAT can do** (today: read-only compat +
  originals). Mitigations: hashed at rest (unchanged), revocation UI exists, device tokens expire after 365 days by default (web-created PATs
  stay non-expiring unless a lifetime is chosen),
  token management and successor-minting excluded from bearer (Decision 5), fail-closed
  precedence (Decision 6), one test per router category, Codex authz gate at `high`, and the
  acceptance probe exercises the 401 paths on prod.
- **Two prod migrations.** Both expand-only (a nullable column; a new table); rollback =
  retagged image + `alembic downgrade -1` is not needed for the app to run on the old image
  (old code ignores the new column/table). Verified per the CARDINAL RULE procedure.
- **Authelia untouched is a claim that must be proven, not asserted.** Phase 2's test asserts
  request equality with web mode, and the deploy verification diffs the live config hash with a
  positive control.
- **Malicious initiator on the same device (T2).** PKCE alone does not stop an app that starts
  its own login and receives the callback; the HTTPS callback + associated-domains entitlement
  does — only the entitled app can receive `/ios/auth/callback`. Codes are single-use (atomic
  `UPDATE`), 120 s, and device tokens expire after 365 days by default.
- **merkur is not build-ready.** No Xcode, 22 GiB free, 16 GB RAM; Xcode 26.6 + one simulator
  runtime need ~30 GB steady. Phases 3–8 wait on the user's disk cleanup + install; Phases 0–2
  deliver value alone (bearer auth also unblocks any future client, e.g. the frame/compat
  consumers). Xcode Cloud keeps release builds off merkur.
- **OpenAPI 3.1 features the Swift generator cannot consume** (validation keywords, `default`,
  cookie params). Phase 3's first action enumerates them; the fix lives in memos-ng's schema
  output, keeping `openapi.json` the single contract.
- **Xcode 27 / iOS 27 ship mid-plan** (Sept 2026) and force Liquid Glass when built with 27. The
  plan pins Xcode 26.6 until Phase 6; the toolchain bump is its own small change.
- **Share-extension memory and single-attach background sessions** are the classic failure
  modes; the design is file-based + hand-off, and the state machine is unit-tested before a
  device is touched.
- **App Review 2.1 for a private server** — only relevant in Phase 8, solved by the demo stack;
  TestFlight internal needs no review.
- **Thermal coupling on Saturn** — uploads from the app produce the same worker load as SPA
  uploads; no new transcode path is introduced.

## Acceptance Criteria

1. A bearer PAT authenticates every route under `/api/*` except `/api/pats/*` and the OIDC web
   endpoints (`/api/auth/login`, `/api/auth/callback`); an expired PAT and an invalid bearer
   alongside a valid cookie both yield 401; pytest cases exist per router category and go red
   when `require_user`'s bearer branch is removed; the dated prod probe (5 steps, Phase 1) is in
   the session review.
2. `tests/test_openapi_drift.py` exists, is green, and was shown red once against a deliberate
   schema change; `openapi.json` + `schema.d.ts` are regenerated and committed.
3. A native login completes from a real browser against production through the existing BFF
   callback to a `https://memos.discomarder.live/ios/auth/callback?code=` URL, the exchange
   yields a PAT that lists memos, and two concurrent exchanges of one code yield exactly one
   token; the live Authelia configuration hash is unchanged (with positive control); no
   `sessions` row is created for an ios-mode login.
4. `memos-ng-ios` exists with the package layout Features → Core → API, Swift 6 mode in every
   target, `xcodebuild test` and `swift format lint --strict` green (bare exit codes cited), the
   markdown/task-toggle pure functions passing the same vectors as the Vue specs, and a
   generated client built from the vendored `openapi.json` with its source SHA recorded; the markdown parity fixture
   (SPA-produced projections) passes in Swift Testing.
5. A TestFlight internal build on two household iPhones performs native login, creates a memo,
   and shares a photo from Photos.app whose upload completes with the app backgrounded —
   evidenced by `uploads`/`attachments` rows and a `thumb` rendition read back from prod, dated.
6. ADR-0015 (memos-ng) and ADR-0001 (memos-ng-ios) are `accepted`; the reference-architecture
   doctrine carries the verbatim verdicts of independent Codex and ChatGPT-Pro reviews and is
   listed in the estate ADR index with a root-relative link; `memos-ng-ios/CLAUDE.md` states the
   gate command, the dependency rule, and the concurrency rule.
7. Every prod deploy in this plan went through `/safe-deploy` with a `rollback-<sha>` retag,
   running Image Id == built Id, `/health` 200 and the phase's named acceptance probe; no
   Authelia or Traefik change occurred.
8. (Phase 8, only if chosen) The unlisted App Store link installs on a non-tester device and
   the demo stack serves the reviewer path over bearer.

## Plan gate record

Codex (`codex-cli 0.144.6`, `--sandbox read-only`, `model_reasoning_effort=high`, session
`01a02cdb-2e76-7273-a8dc-2bae7d07fffc`), 2026-08-23:

- Round 1 — REVISE: 4 BLOCKING (custom-scheme callback not bound to the legitimate app under
  PKCE; non-atomic one-time-code consumption; Phase 2 missing schema regeneration / Phase 4
  resync; iOS markdown contract lacked rendering + link-safety semantics) + 6 ADVISORY (all
  folded except "require an iOS-26-only need" — declined with the stated reason).
- Round 2 — REVISE: 3 BLOCKING (settings read once → AASA enablement needs a same-image
  recreate; parity fixture needs a declared memos-ng branch; rsync gate vs. user's Xcode edits
  needed an exchange contract). All folded.
- Round 3 — **APPROVE**, zero blocking, zero advisory.

Estate ADR index (page 2697) checked 2026-08-23: portainer-stacks ADR-0002 (estate-scope) (`/link/1429`, OIDC for
mobile-capable apps) honoured; bookstack-ng ADR-0002 (`/link/1432`) cited as a project-scoped
precedent for a different client type; no estate ADR settles a web or iOS stack.

Phase 3 premise gate (session `01a02ddc-c5a9-7af2-9911-99e9005df77e`): round 1 REVISE (6
BLOCKING — fixture inputs/structure, write-separation, happy-dom vs DOMPurify, mac-gate contract,
scheme test targets, keychain entitlement; all folded), round 2 REVISE with 3 precision items
(inline marks in the projection, Swift Testing count source, entitlement phase ownership — folded)
+ advisories; no round 3 per round discipline — the post-phase gate on the diff is next.

Phase 2 premise gate (session `01a02d1a-36d1-7211-96d7-1ea1162e0e37`): round 1 REVISE (3
BLOCKING — cleanup vs. classification, authorize-URL equality, route precedence; all folded),
round 2 REVISE with one marginal blocker (HEAD vs GET in the production probe — folded) and 3
advisories (folded into the implementer brief). No round 3 run: per round discipline the
remaining findings were marginal; the post-phase code gate on the actual diff is the next check.
