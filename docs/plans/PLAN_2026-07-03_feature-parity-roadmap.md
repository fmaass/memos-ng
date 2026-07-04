---
name: "Feature-parity roadmap — memos-ng vs memos 0.29.1"
overview: "Critical review of what the migration to memos-ng lost versus the original usememos/memos 0.29.1, and a prioritized, phased roadmap to close the gaps that matter. Central decisions: tiered priorities by household value (not upstream feature count), comments modeled as child memos, a server-settings table for admin config, and explicit non-goals for upstream features that never existed, were removed upstream, or were deliberately dropped for security."
scope: major
project: "memos-ng"
feature: "feature-parity-roadmap"
status: draft
created: "2026-07-03"
todos:
  - id: "tier-a-quick-wins"
    content: "Tier A — editor/search/tag quick wins (chips, autocomplete, highlight, emoji, tag mgmt, shortcuts)"
    status: pending
  - id: "tier-b-collaboration"
    content: "Tier B — comments, relations, mentions, inbox (the collaboration core)"
    status: pending
  - id: "tier-c-sharing"
    content: "Tier C — share links, share-as-image, RSS"
    status: pending
  - id: "tier-d-content"
    content: "Tier D — math, mermaid, link previews, geotag, nested tags"
    status: pending
  - id: "tier-e-platform"
    content: "Tier E — admin settings UI, members view, per-user prefs/i18n, webhooks, MCP"
    status: pending
isProject: true
---

# Implementation Plan: Feature-parity roadmap — memos-ng vs memos 0.29.1

## Context for Independent Review

- **Current state:** memos-ng is a from-scratch FastAPI + Vue 3/PrimeVue replacement
  for `neosmemo/memos:0.29.1`, in production at `memos.discomarder.live` since the
  2026-06-21 cutover (repo main at v1.1.0, `232e66d`). Two full inventories were
  produced on 2026-07-03 and ground this roadmap: (1) an exhaustive code-level
  inventory of memos-ng (every router in `app/routers/`, every view in
  `frontend/src/views/`), and (2) a source-cited feature inventory of upstream
  memos v0.29.1 (usememos.com docs, the v0.29.1 git tag protos/package.json, local
  fixtures `fixtures/live-0.29.1/`). memos-ng today has: GFM-subset markdown
  (task lists with interactive toggle, tables, code blocks **without** syntax
  highlighting), flat `#\w+` tags with sidebar counts, PRIVATE/PROTECTED/PUBLIC
  visibility, pin/archive, emoji reactions (fixed 4-emoji picker), explore feed,
  calendar heatmap, full-text search (`q` only — no UI filter chips), resumable
  chunked uploads with a tiered media pipeline (thumb/mobile/web/poster/print +
  HEIC + blurhash), PhotoSwipe lightbox, resources library, OIDC BFF auth, PATs
  with management UI, per-user settings (default visibility, theme, avatar),
  web push + full PWA (service worker), markdown zip export, read-only
  `/api/v1` + `/file` compat for satellites (daily-mail, slideshow), Prometheus
  metrics, and a poll-based SSE media-status channel (`GET /api/events`,
  `app/routers/assets.py`).
- **Problem / motivation:** the migration prioritized the household's core flows
  (capture, media, timeline) and dropped a long tail of upstream features. The user
  has asked for a critical review of what was lost and a detailed roadmap to bring
  those features to memos-ng.
- **Verified gap list (lost in migration — confirmed absent in memos-ng code, present
  in upstream 0.29.1):** comments on memos (upstream: nested, comment-is-a-memo via
  COMMENT relation, inbox previews); memo relations/references (upstream:
  REFERENCE relations + link-memo dialog; our compat surface hardcodes
  `relations: []`); @username mentions with inbox notifications; the notifications
  inbox itself; shortcuts (saved CEL filters, dedicated page since 0.29.0); code
  syntax highlighting (upstream highlight.js); LaTeX/KaTeX math; Mermaid diagrams;
  link-preview metadata cards (0.29.0, with DNS-rebinding protection);
  location/geotag on memos (Leaflet picker + profile map); voice notes + AI
  transcription (0.27+); hierarchical/nested tags with tag tree; tag
  rename/delete; per-tag color/blur admin metadata; time-limited share links
  (0–30 day expiry, work anonymously for non-public memos); share-as-image; RSS
  (explore-wide + per-user); admin/instance settings UI (upload cap, allowed
  reaction set, branding/custom profile — memos-ng has env vars only, and a stored
  but ungated `role` field); user-management/members view; per-user preferences
  beyond visibility/theme/avatar (locale, nickname/description, created-vs-updated
  display timestamp); i18n (upstream ships 33 locales; memos-ng is English-only);
  per-user webhooks on memo events (SSRF-guarded upstream); built-in MCP server
  (PAT-authenticated, 0.27+); UI search filters (tag/visibility/date chips — our
  backend supports these filters, the UI exposes only free-text); editor
  conveniences (tag autocomplete — planned T3.2, never built; slash commands;
  auto-title; outline; draft preservation); full emoji-reaction set
  (admin-configurable upstream vs our hardcoded 4).
- **Where memos-ng is AHEAD of upstream (do not "fix" these):** markdown zip export
  (upstream has no export at all), resumable uploads with wake-lock, the tiered
  media pipeline + blurhash + print renditions + HMAC-signed URLs, HEIC handling,
  true PWA with offline app-shell + web push (upstream has neither service worker
  nor push), Prometheus metrics, thermal-aware dual-lane worker, and the
  BFF/httpOnly session model (upstream stores tokens client-side).
- **Ghosts and deliberate drops (explicit non-goals — do not chase):** memo edit
  history (never existed upstream); daily/random review (removed upstream pre-0.22);
  Telegram integration (external Memogram since 0.22); relation *graph
  visualization* (added 0.23, gone by 0.29); raw HTML / trusted-iframe rendering
  in memo content and instance-level custom CSS/JS injection (memos-ng deliberately
  renders with `html:false` + DOMPurify and CSP `frame-ancestors 'none'` — a
  security posture we keep); the full memos v1 *write* API and dead compat fields
  (settled in the original plan's API-hygiene decisions); on-the-fly image
  resizing (replaced by tiers, settled); voice notes + AI transcription is
  **deferred, not planned** (needs a separate product decision on AI providers and
  a use case — parked in the backlog, not a roadmap phase).
- **Scope boundary:** IN — a prioritized roadmap of the verified gap list above,
  organized into five tiers that each become one or more standard `/archimedes-plan`
  implementation plans when picked up; per-feature intent, key design decisions,
  and acceptance sketches. OUT — implementing anything in this document (it is a
  roadmap, not a build plan); the ghosts/drops above; changes to the read-only
  compat surface contract (fixtures stay ground truth); the Aura frame email
  integration (separate approved plan, PLAN_2026-07-03_aura-frame-email.md).
- **Hard constraints:** production system — every tier lands via feature branches,
  CI green on all jobs (backend, frontend, api-drift, release), tests-first per
  project CLAUDE.md, phase gates with `/converge` at each tier close, `/safe-deploy`
  for every deployment; compat fixtures (`fixtures/live-0.29.1/`) must keep passing
  — parity features must not change the satellite-facing contract (exception:
  populating `relations` in compat output once relations exist is additive and
  fixture-compatible only if existing fixtures still pass verbatim); DB is shared
  Postgres17 on Saturn (migrations additive; no long table rewrites on hot tables
  without a maintenance note); the per-user settings pattern is `User` columns +
  `/api/settings` (small prefs) but Tier E introduces a proper `server_settings`
  table for instance-level admin config (a deliberate, ADR-worthy addition).
- **Terminology:** "upstream" = usememos/memos v0.29.1. "Tier" = a priority cluster
  of features in this roadmap, not a media tier. ADR-0008 (HEIC archive masters),
  ADR-0009 (HMAC-signed media URLs), ADR-0010 (feeds architecture stays) remain
  binding; nothing here re-litigates them.

## Goal

Give the household a deliberate, prioritized path to restoring the upstream features
lost in the migration — highest daily value first, collaboration features as a
coherent block, and platform/admin work last — with each tier sized, sequenced, and
specified well enough to be picked up as its own gated implementation plan, and with
an explicit, argued list of upstream features we will *not* rebuild.

## Approach

**Prioritize by household value, not upstream parity count.** The review found ~26
real gaps. They cluster into five tiers ordered by (daily value ÷ effort), with
collaboration features kept together because they share schema (relations) and
surfaces (memo detail/thread view, inbox).

**Explicit household stop-point (guidance, not a hard gate).** Tiers A and B deliver
nearly all the day-to-day value: A restores the editor/search/tag ergonomics the
household touches constantly, and B restores the only genuinely *missing capability*
(a memo detail view + comments/relations/mentions/inbox — collaboration). **Treat
"done" as the end of Tier B.** Tier C (sharing) is worth doing if the household
actually wants public/expiring links or RSS; Tiers D (content richness) and E
(platform/admin) are **demand-driven** — pick individual items when a real need
appears, not for parity's sake. This roadmap deliberately does not commit to
finishing all five tiers; it commits to A→B and makes C/D/E available, sequenced,
and pre-specified so any one can be pulled forward cheaply. This avoids "parity for
parity's sake" on a two-user instance. Each tier closes with `/converge` and a
deployment; each numbered feature gets its own `feature/<slug>` branch and, for
standard/major items, its own `/archimedes-plan` with the mandatory Codex gate. This
document is the roadmap of record; BookStack gets it as the durable copy, and the
superseded parity memory (`parity-scope`) is updated to point here.

**Key cross-cutting design decisions locked at roadmap level (details per-plan):**

1. **Comments are child memos** (upstream-compatible mental model): a nullable
   `memos.parent_id` FK + relation kind, not a separate comments table — comments
   inherit the memo machinery (markdown, attachments, reactions, visibility from
   parent) and the compat surface can expose them later without remodeling. This
   requires memos-ng's first **memo detail view** (route `/m/{uid}`), which Tier B
   builds first because comments, relations, and mentions all render there.
2. **Relations are a first-class table** (`memo_relations {memo_id, related_memo_id,
   kind REFERENCE|COMMENT}`) — one schema serves comments (kind=COMMENT via
   parent_id or relation row; the Tier B plan picks one and justifies it), reference
   links, and the compat `relations` array.
3. **Notifications ride the existing web-push + a new `notifications` table** (the
   inbox): comment/mention events write an inbox row and fire the existing
   `push.send_to_user` — no new delivery infrastructure.
4. **Server-level admin settings get a `server_settings` table** (single-row or
   key-value; Tier E decides) with an admin-gated router — the stored `role` field
   finally gates something. Upload cap, allowed reaction emoji, instance branding
   move from env/hardcode to DB with env as bootstrap default.
5. **Markdown extensions stay behind the existing sanitizer discipline:** syntax
   highlighting, KaTeX, and Mermaid render client-side in the existing
   markdown-it + DOMPurify pipeline (Mermaid in a sandboxed render path — it is the
   riskiest; its plan must include an XSS review), never via raw HTML pass-through.
6. **Link previews and webhooks — both fetch/deliver to user-supplied URLs — ship
   with SSRF guards from day one** (private-IP/CIDR deny by default, matching
   upstream's posture), reusing one shared egress-guard helper.
7. **Search chips and shortcuts share one filter model:** the UI chips (A1)
   serialize to the same query-param grammar the backend already supports; saved
   shortcuts (A6) persist exactly that serialization per user — no CEL engine is
   rebuilt (that stays a compat-only concept). A6 is sequenced right after A1 in
   Tier A because it is a thin persistence layer over A1's serialization.

### Tier A — Quick wins: search, editor, tags (highest value ÷ effort)

- **A1. Search filter chips + date range** — UI over existing backend filters
  (`tag`, `visibility`, `created_before/after`, media-type via `has`-style filter if
  cheap). `SearchView.vue` + `HomeView` tag param unification.
- **A2. Tag autocomplete in the editor** (`#` triggers a popup fed by `GET
  /api/tags`) — was planned (old T3.2), never built.
- **A3. Code-block syntax highlighting** — highlight.js (or Shiki if bundle cost
  acceptable) wired into `renderMemoMarkdown`; language auto-detect off, fence
  info-string only; unit tests on the pure renderer.
- **A4. Configurable reaction set** — replace the hardcoded 4-emoji picker with a
  fuller picker; the *allowed set* becomes admin-configurable only when Tier E's
  server settings exist (until then: a larger sensible constant).
- **A5. Tag rename/delete** — `PATCH /api/tags/{name}` rewrites `#old` → `#new` in
  owned memo content inside one transaction (regex-boundary-safe, per-category
  tests); delete = strip tag from content. Both owner-scoped; tag rows are derived,
  so content is the source of truth.
- **A6. Saved shortcuts (saved filters)** — `shortcuts {user_id, name, filter}` +
  `GET/POST/DELETE /api/shortcuts`; each stores the exact A1 filter serialization
  (decision 7 — no CEL engine). Sidebar "Shortcuts" section under Tags in
  `AppShell.vue`; clicking one applies the saved filter to the timeline. Sequenced
  after A1 (thin persistence layer over its serialization). Upstream parity for the
  0.29.0 Shortcuts page, sized to a household (no CEL validation UI).

### Tier B — Collaboration core: detail view, comments, relations, mentions, inbox

- **B0. Memo detail view** (`/m/{uid}` route + deep links from cards) — prerequisite
  surface for everything below; also gives public memos a real permalink.
- **B1. Comments** — child memos (decision 1/2 above): compose box + thread on the
  detail view, comment counts on cards, visibility inherited from parent (upstream
  0.29 semantics), delete cascades. Compat: comments are NOT exposed to satellites
  (fixtures unchanged).
- **B2. Memo relations (references)** — link-memo UI (search-and-pick dialog),
  relation cards on detail view, `relations` populated in clean API; compat keeps
  `relations: []` unless fixtures prove existing consumers tolerate content
  (default: leave compat untouched).
- **B3. @mentions** — `@username` parsed like tags (server-side), renders as a
  profile link, writes an inbox notification.
- **B4. Inbox** — `notifications` table + `/api/notifications` + bell/inbox view;
  comment + mention events; existing web push fires on new rows (decision 3).

### Tier C — Sharing

- **C1. Time-limited share links** — `memo_shares {memo_id, token, expires_at}`;
  anonymous route `GET /s/{token}` renders a read-only memo (media via the existing
  signed-URL mechanism with matched expiry); create/revoke UI on the detail view;
  0–30 day expiry like upstream.
- **C2. Share as image** — client-side render of a memo card to PNG (canvas/
  html-to-image); pure-function extraction where testable; no server component.
- **C3. RSS** — `/explore/rss.xml` + `/u/{username}/rss.xml`, PUBLIC memos only,
  unauthenticated; ensure Traefik/Authelia pass these paths (mirror the sanitized
  template the same session, per house rule).

### Tier D — Content richness

- **D1. KaTeX math** (`$…$`/`$$…$$` via markdown-it plugin, CSS bundled).
- **D2. Mermaid diagrams** — sandboxed client render, strict security review in its
  plan (worst XSS surface of the roadmap).
- **D3. Link-preview cards** — server-side `GET /api/link-metadata?url=` with the
  shared SSRF guard (decision 6), cached in DB, rendered as cards.
- **D4. Location/geotag** — `memos.location {lat, lon, label}` columns, Leaflet
  picker + display (household value: photos already carry `captured_at`; a map
  view of memos is the actual win — scope the map into this item).
- **D5. Nested tags** — extend `TAG_RE` to `#a/b`, tag tree in sidebar, prefix
  filtering; migration-safe (existing flat tags are valid leaves).

### Tier E — Platform & admin

- **E1. `server_settings` table + admin router + admin settings page** — upload cap,
  allowed reactions, instance branding; `role` finally gates (`require_admin`).
  ADR required (settings architecture).
- **E2. Members view** — read-only user list for admin (name, avatar, memo count,
  last seen), no user CRUD (Authelia owns identity).
- **E3. Per-user prefs + i18n** — locale (vue-i18n, EN + DE only to start),
  nickname, created-vs-updated timestamp display preference.
- **E4. Per-user webhooks** — memo created/updated/deleted events, HMAC-signed
  payloads, shared SSRF guard, per-user CRUD in settings. (Also the generic
  successor to bespoke integrations like the Aura mailer — the Aura plan ships
  first and stands alone; webhooks do not replace it.)
- **E5. MCP server** — PAT-authenticated MCP endpoint exposing memo search/create
  tools (mirrors upstream 0.27+); high household leverage given Claude-centric
  tooling, but last because it rides on stable APIs.

## Rejected Alternatives

- **Rebuild everything to upstream parity in upstream's own shapes** (CEL filter
  engine, gRPC-gateway API, comment-as-relation-only model): memos-ng's clean REST
  + typed OpenAPI pipeline is the settled architecture; parity is measured in user
  capability, not API shape. CEL stays compat-only.
- **A separate `comments` table:** loses markdown/attachments/reactions on comments
  and diverges from upstream's proven model; child memos reuse everything.
- **Server-Sent-Events-based inbox delivery:** the household already runs web push
  end-to-end (worker → service worker); an SSE channel adds a second delivery path
  for no reach gain (push works with the PWA closed).
- **Implementing voice notes + AI transcription now:** real upstream feature, but
  needs an AI-provider decision (keys, cost, privacy) the household hasn't made;
  parked as backlog, not roadmap.
- **i18n with the full 33-locale surface:** translation debt for a 2-locale
  household; EN + DE covers actual users, the framework leaves the door open.
- **Restoring raw-HTML/iframe markdown and custom CSS/JS injection:** deliberate
  security downgrade with no household demand; upstream keeps them for public
  instances — we are not one.

## Phased Breakdown

Each tier below is a roadmap phase. A tier is picked up by writing its per-feature
implementation plan(s) via `/archimedes-plan` (standard scope each, with the Codex
plan gate), implementing under that plan's own Execution Protocol, and closing the
tier with `/converge`. Order within a tier is the listed order unless its plan argues
otherwise. **This roadmap document itself has no code deliverables.**

### Phase 1 — Tier A (quick wins)
- **Branch:** one `feature/<slug>` per item (A1–A6 are independently shippable; A6
  sequenced after A1).
- **Build:** items A1–A6 as specified above; A5 (tag rename) is the only one with
  data-integrity risk and needs per-category rewrite tests (tag at start/middle/end,
  substring-collision `#work` vs `#workout`, code-block spans); A6 reuses A1's
  filter serialization verbatim.
- **Do NOT:** build a CEL validation engine for shortcuts (A6 persists the plain A1
  serialization only), admin-configurable reaction sets (needs E1), or touch compat.
- **Verification:** each item's own plan gates; tier closes with `/converge` and a
  deployed, live-verified build; heatmap/search/editor regressions covered by
  existing + new vitest/pytest.
- **Status:** pending

### Phase 2 — Tier B (collaboration core)
- **Branch:** `feature/memo-detail`, then `feature/comments`, `feature/relations`,
  `feature/mentions-inbox` (B0 strictly first).
- **Build:** B0–B4 as specified; schema decisions (parent_id vs relation-row for
  comments) argued in the Tier B plan with an ADR; notifications table + push wiring.
- **Do NOT:** expose comments/relations through `/api/v1` compat (fixtures are the
  contract); build threaded-comment nesting deeper than one level unless the plan
  argues for it; add email notifications.
- **Verification:** compat fixture tests still green verbatim; per-feature test
  suites (visibility inheritance per category, cascade deletes, mention parsing
  categories); `/converge` at tier close.
- **Status:** pending

### Phase 3 — Tier C (sharing)
- **Branch:** `feature/share-links`, `feature/share-image`, `feature/rss`.
- **Build:** C1–C3; C1's anonymous route needs a security pass (token entropy,
  expiry enforcement, signed media URL lifetime alignment); C3 requires the
  Traefik/Authelia rule change mirrored into the in-repo sanitized template.
- **Do NOT:** allow share links on PRIVATE memos of other users; add write access
  via share tokens; build Atom variants.
- **Verification:** anonymous-access tests per visibility category; expiry tests;
  live RSS fetch from an unauthenticated client post-deploy; `/converge`.
- **Status:** pending

### Phase 4 — Tier D (content richness)
- **Branch:** one per item D1–D5.
- **Build:** D1–D5 as specified; D2 (Mermaid) and D3 (link previews/SSRF) carry the
  security weight — their plans must include an explicit abuse-case section.
- **Do NOT:** enable raw HTML anywhere; fetch link metadata client-side; store
  geodata on attachments (memo-level only, per upstream).
- **Verification:** renderer unit tests per syntax category (including hostile
  inputs); SSRF guard tests (private CIDRs, redirects, DNS rebinding); `/converge`.
- **Status:** pending

### Phase 5 — Tier E (platform & admin)
- **Branch:** one per item E1–E5; E1 first (E2–E4 depend on admin gating or the
  settings table).
- **Build:** E1–E5 as specified; E1 ships the `require_admin` dependency and the
  ADR; E4 reuses the D3 SSRF guard; E5's MCP endpoint is PAT-scoped read+create
  initially.
- **Do NOT:** build user CRUD (Authelia owns identity); expose webhooks or MCP
  without auth; migrate env-var config away silently (env stays the bootstrap
  default, DB overrides).
- **Verification:** admin-gating tests (role categories: admin, user, anonymous);
  webhook delivery + SSRF tests; MCP exercised end-to-end with a real PAT;
  `/converge`; final roadmap retrospective + `/reflect`.
- **Status:** pending

## Execution Protocol (Agentic Review Loop)

This roadmap is a planning artifact; its "execution" is the creation and delivery of
per-feature implementation plans. The protocol therefore operates at two levels:

1. **Roadmap gate (this document):** adversarial self-check (done), then the Codex
   cross-model review loop until APPROVE — write the reviewer prompt to a file, run
   `codex exec --sandbox read-only - < prompt.txt` (stdin redirected), demand
   independent repo analysis first and a typed verdict (`VERDICT: APPROVE | REVISE`
   + file:line evidence) in a single fenced block at the end, parse the LAST
   occurrence; delta rounds via
   `codex exec --sandbox read-only resume <session-id> - < delta.txt`.
2. **Per-feature plans:** every standard/major item spawned from this roadmap embeds
   the full three-gate loop from the implementation-plan template (pre-execution
   Codex gate on the phase spec → implementation with local gates green
   (`uv run pyright app/ && uv run ruff check app/ tests/ scripts/ && uv run pytest
   -q && uv run alembic check`, frontend vitest/build) → post-phase Codex diff gate
   via `~/.claude/bin/agent-review-orchestrator.py` or the self-contained
   `codex exec --sandbox read-only` fallback with real command output pasted in).
   Before merge of each feature: final cross-model audit against that plan's
   acceptance criteria. Tier close: `/session-review` + `/implementation-report`,
   then `/converge` (L1–L4 validators + Codex). If Codex is unavailable at any
   gate, substitute a fresh Claude subagent in an isolated worktree with the same
   adversarial prompt — never skip a gate. Before any tier's first implementation
   session, invoke `/disciplined-implementation`.

## Dependencies

- The Aura frame email plan (PLAN_2026-07-03_aura-frame-email.md, approved) executes
  independently and first — it shares no schema with this roadmap.
- Active media plans 1667 (per-memo originals layout — deploy pending) and 1665
  (screen/print renditions) should reach their own gates independently; nothing in
  Tiers A–E touches the media pipeline.
- User decisions required before Tier E: locale set (assumed EN+DE), whether MCP
  (E5) ranks above webhooks (E4).

## Dependencies on Current State

- Backend surfaces this roadmap extends: `app/routers/memos.py` (memo CRUD, reactions,
  tags, heatmap), `app/routers/settings.py` (per-user settings), `app/routers/assets.py`
  (signed URLs, SSE), `app/services/memos.py` (`parse_tags`, `create_memo`,
  `update_memo`, `link_attachments`), `app/services/push.py` (web push),
  `app/models.py` (Memo, Tag, memo_tags, User, Attachment), Alembic migrations.
- Frontend surfaces: `frontend/src/lib/markdown.ts` (`renderMemoMarkdown` — the pure
  renderer all markdown extensions hook into), `SearchView.vue`, `SettingsView.vue`,
  `CaptureEditor.vue`, `MemoCard.vue`/`MemoFeed.vue`, `AppShell.vue` sidebar,
  TanStack Query composables, OpenAPI → `schema.d.ts` generation (api-drift CI).
- Compat ground truth: `fixtures/live-0.29.1/` + `NOTES.md` (F1: unauth = 200
  PUBLIC-only; F2: original tier 404 over `/file/`) — must keep passing verbatim.
- Infra: Traefik/Authelia config on Saturn (only Tier C RSS and possibly share-link
  routes touch it; mirror to in-repo templates same session).
- BookStack: roadmap of record page 1649 (book 1648) documents the original build
  plan; this roadmap is the post-cutover successor and must be persisted alongside
  it, and the `parity-scope` memory updated to point here.

## Risks

- **Scope gravity:** five tiers ≈ 20+ features; the roadmap only works if each tier
  closes (converged, deployed) before the next opens — same phase discipline as the
  original build. Mitigation: tier gates are hard; this document tracks per-tier
  status.
- **Comments schema lock-in (B1):** child-memo modeling touches the hot `memos`
  table and its list queries (must exclude comments from timelines). Mitigation:
  the Tier B plan carries an ADR + query-plan evidence on the timeline hot path
  (ADR-0010 territory — the feeds architecture is settled and must not regress).
- **Sanitizer regressions (A3, D1–D3):** every markdown extension widens the XSS
  surface. Mitigation: all rendering stays inside `renderMemoMarkdown` + DOMPurify;
  hostile-input tests per extension; Mermaid sandboxed and reviewed hardest.
- **SSRF via link previews/webhooks (D3, E4):** upstream had to harden both.
  Mitigation: one shared egress guard, deny-by-default private CIDRs, redirect and
  DNS-rebinding tests, no client-side fetching.
- **Anonymous surfaces (C1, C3):** share links and RSS open unauthenticated routes
  on a household system. Mitigation: token entropy ≥128 bits, expiry enforced
  server-side, PUBLIC-only for RSS, security pass in each plan, Authelia bypass
  rules scoped to exact paths.
- **Compat drift (B2):** populating `relations` for satellites could break
  daily-mail/slideshow parsing. Mitigation: compat stays frozen to fixtures;
  enrichment only in the clean API.
- **i18n cost (E3):** translation is a long tail. Mitigation: EN+DE only; keys
  extracted incrementally per view.

## Acceptance Criteria

This roadmap (the document, not the features) is done when:

1. The gap list is complete and evidence-based — every upstream 0.29.1 feature is
   classified as: present, ahead, gap (tiered A–E), deferred (voice/AI), or
   non-goal (ghosts/drops) — with the two source inventories retained.
2. The roadmap passes the Codex cross-model review (APPROVE) and user approval on
   tier ordering and the non-goal list.
3. It is persisted to BookStack under the memos-ng shelf as an implementation-plan
   page (tagged `[archimedes:implementation-plan] [archimedes:project=memos-ng]
   [archimedes:status=active]`), alongside the local `docs/plans/` copy, and the
   `parity-scope` memory is updated to reference it.
4. Tier A's first per-feature implementation plan can be started directly from this
   document with no additional discovery (files, patterns, and decisions named).

Feature-level acceptance lives in each tier's own plans; tier completion = all its
items converged and deployed, this document's phase status updated.
