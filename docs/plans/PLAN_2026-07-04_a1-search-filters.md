---
name: "A1 — Search filter chips + date range"
overview: "A frontend-only filter UI over the memo timeline: tag/visibility/date-range/archived chips plus free-text, backed by a pure FilterState↔MemoQuery↔URL serialization module that A6 (saved shortcuts) will persist verbatim. No backend, schema, or API change."
scope: standard
project: "memos-ng"
feature: "a1-search-filters"
status: shipped (frontend/src/components/FilterBar.vue; #28)
created: "2026-07-04"
todos:
  - id: "a1-serialize"
    content: "Pure filter-serialization module + vitest (FilterState↔MemoQuery↔URL)"
    status: pending
  - id: "a1-filterbar"
    content: "FilterBar.vue chips component (tag/visibility/date/archived/free-text)"
    status: pending
  - id: "a1-wire"
    content: "Wire SearchView + unify HomeView ?tag onto the shared model; URL sync"
    status: pending
isProject: false
---

# Implementation Plan: A1 — Search filter chips + date range

## Context for Independent Review

- **Current state (verified 2026-07-04):** memos-ng is the production Memos replacement
  (main at `4a7352a`). The memo list endpoint `GET /api/memos`
  (`app/routers/memos.py:180-215`) ALREADY accepts every filter this item needs:
  `created_before`, `created_after` (datetime), `tag` (repeatable → `list[str]`),
  `visibility` (enum PRIVATE|PROTECTED|PUBLIC), `owner`, `q` (free-text), `include_archived`,
  `archived_only`, `sort` (default `-created_at`), plus cursor/limit/with_total. The frontend
  reaches it through `useTimeline(query)` (`frontend/src/composables/useTimeline.ts`), whose
  `MemoQuery = Record<string, string | boolean | string[]>` maps 1:1 onto those query params
  and is passed straight to the typed openapi-fetch client. Today only two callers build a
  MemoQuery: `SearchView.vue` (39 lines — a single debounced free-text `q` and nothing else)
  and `HomeView.vue` (reads `?tag=` from the route into `q.tag = [tag]`). `ArchivedView.vue`
  passes `{ archived_only: true }`. There is NO filter UI (chips, date range, visibility
  picker) anywhere, and no shared filter serialization — each view hand-rolls its MemoQuery.
- **Problem / motivation:** upstream memos exposes tag/visibility/date filtering in its
  timeline; memos-ng lost it in the migration (the backend supports it, the UI does not).
  This is roadmap item **A1** (Tier A, quick wins). A1 is also the foundation for **A6 (saved
  shortcuts)**: decision 7 of the roadmap locks that shortcuts persist *exactly* A1's filter
  serialization — no CEL engine, no second grammar. So A1's serialization is a public contract
  A6 depends on, not a private detail.
- **Scope boundary — IN:** (1) a pure, dependency-free serialization module
  (`frontend/src/lib/memoFilter.ts`) with a `MemoFilter` state object and total functions
  `filterToQuery(f): MemoQuery`, `filterToSearchParams(f)/filterFromSearchParams(sp)` (URL
  round-trip), and `isEmptyFilter(f)` — unit-tested directly in vitest per input category;
  (2) a `FilterBar.vue` presentational component (tag multiselect fed by existing `useTags`,
  visibility select, date-range pickers, an archived toggle, free-text input, a clear-all)
  emitting `MemoFilter`; (3) wiring it into `SearchView.vue`, and refactoring `HomeView.vue`'s
  `?tag=` handling onto the same module so both views share one serialization; (4) reflecting
  the active filter into the URL query string so a filtered view is linkable and survives
  reload. **OUT (explicit):** any backend/schema/API/OpenAPI/migration change (backend filters
  already exist — do not touch them); saved shortcuts persistence (that is A6 — this plan only
  makes the serialization A6 will consume); a `has:media`/media-type filter (backend has no
  such param — deferred unless trivially free, and it is not, so OUT); full-text search ranking
  changes; the compat surface (`/api/v1`, `/file/*`) and `fixtures/live-0.29.1/` (frozen).
- **Hard constraints (settled — do not re-litigate):** never commit to `main` — work on
  `feature/a1-search-filters`, squash-merge via PR after CI green on ALL jobs (backend,
  frontend, api-drift, release). Tests-first, real not fake (removing the logic turns a test
  red; never assert a self-constructed value). **House rule (project CLAUDE.md): extract
  security/logic-critical behaviour into a pure function and unit-test it DIRECTLY — do not
  rely on driving it through a component in happy-dom** (which lacks EventSource/canvas/
  clipboard/localStorage); hence the serialization lives in `lib/memoFilter.ts`, not inside the
  component. PrimeVue 4 (Aura) components only; TanStack Query for server-state (no Pinia).
  Frontend types are generated from OpenAPI — but this plan adds NO endpoint, so `schema.d.ts`
  and `openapi.json` must remain byte-identical (api-drift CI job stays green precisely because
  nothing backend changed). `MemoQuery` already types the param bag — reuse it, do not
  redefine it.
- **Terminology:** *MemoFilter* = the UI-facing filter state object this plan introduces (tags,
  visibility, dateFrom, dateTo, archived mode, text). *MemoQuery* = the existing param bag
  (`useTimeline.ts:6`) sent to `/api/memos`. *Serialization* = the total mapping
  MemoFilter → MemoQuery (for the query) and MemoFilter ↔ URLSearchParams (for the address bar);
  A6 will persist the URL-string form. "Chips" here = the filter controls/pills in the filter
  bar, not memo tags themselves.
- **Hard-won constraints from prior sessions:** happy-dom cannot exercise real component I/O, so
  the testable core is the pure module. Date handling is the classic footgun — the backend applies
  **strict** comparisons: `Memo.created_at < created_before` and `Memo.created_at > created_after`
  (verified `app/services/memos.py:201-203`), NOT `<=`/`>=`. The picker therefore offers
  **day-granularity** range filtering with a single, explicitly-documented boundary convention
  (NOT exact set-theoretic inclusivity, which strict `>` cannot express without an epsilon —
  and an epsilon would leak the previous day). Computed in the user's LOCAL timezone (DatePicker
  yields a local calendar date; `created_at` is tz-aware UTC): **dateTo = D → `created_before` =
  start of local day (D+1)** as a UTC ISO instant — every memo through the end of day D satisfies
  `< that` (upper bound is exact, all of day D included); **dateFrom = D → `created_after` =
  start of local day D** — every memo strictly after that instant matches. Chosen convention,
  stated plainly and tested as intentional: the lower bound is start-of-day and, because the
  backend uses strict `>`, a memo at *exactly* local midnight of the start day is EXCLUDED (a
  knife-edge tie). There is deliberately **no epsilon and therefore no previous-day spillover** —
  cleaner than a `−1 ms` fudge and safe to pin as the A6 serialization contract. An empty date
  drops the param entirely (an empty string would 422). These exact ISO boundary instants are
  what the per-category unit tests pin.

## Goal

Give the household back tag/visibility/date filtering on the memo timeline through a filter bar,
built on a single pure serialization module that both Search and Home share now and that A6
(saved shortcuts) will persist verbatim — with zero backend change, because every filter already
exists on `GET /api/memos`.

## Approach

**One pure module owns the mapping.** `frontend/src/lib/memoFilter.ts` defines
`interface MemoFilter { text?: string; tags: string[]; visibility?: Visibility; dateFrom?: string;
dateTo?: string; archived?: 'exclude' | 'include' | 'only' }` and total functions:
`emptyFilter()`, `isEmptyFilter(f)`, `filterToQuery(f): MemoQuery` (drops absent fields; maps the
day-granularity dates to the day-boundary instants described above — `dateTo`→`created_before` at
start of the next local day, `dateFrom`→`created_after` at start of the local day (no epsilon, so
no previous-day spillover); maps `archived` to `include_archived`/`archived_only`), and the URL
pair
`filterToSearchParams(f): URLSearchParams` / `filterFromSearchParams(sp): MemoFilter`. Tags are
serialized as **repeated `tag` keys** (matching the backend's repeatable `list[str]` param and
`MemoQuery`), never comma-joined, so tag values with commas/spaces round-trip losslessly.
`filterFromSearchParams` accepts both a `URLSearchParams` and Vue Router's `LocationQuery`
(`route.query` is a `Record<string, string|string[]|null>`, NOT a `URLSearchParams`) via a tiny
internal adapter, so Phase 3 can feed it `route.query` directly. The URL form is the canonical
string A6 will store. `Visibility` is imported from the generated `schema.d.ts` enum (uppercase
`PRIVATE|PROTECTED|PUBLIC`), so the picker can't drift from the backend. Everything here is synchronous, pure, and
unit-tested without a DOM.

**A presentational FilterBar.** `frontend/src/components/FilterBar.vue` takes a `MemoFilter`
(v-model) and renders PrimeVue Aura controls: a tag `MultiSelect` populated from the existing
`useTags()` composable, a visibility `Select`, two `DatePicker`s (from/to), an archived
`SelectButton` (Exclude/Include/Only), the free-text `InputText` (debounced, preserving today's
300 ms behaviour), and a "Clear" button shown when `!isEmptyFilter`. It emits filter changes; it
holds no query logic — it just edits a MemoFilter. Active non-text filters also surface as
removable pills for at-a-glance clarity.

**Views become thin.** `SearchView.vue` holds a `MemoFilter` ref, renders `<FilterBar>`, computes
`filterToQuery(filter)` for `<MemoFeed :query>`, and shows the feed whenever the filter is
non-empty (today's "type to search" gate generalizes to "any active filter"). `HomeView.vue`'s
bespoke `?tag=` block is replaced by `filterFromSearchParams(route.query)` so a sidebar tag click
still works and now composes with other filters. Both views sync the active filter into the URL
(`router.replace({ query })`) so filtered timelines are linkable and reload-stable — the same URL
serialization A6 persists. The `['memos', …]` query-key behaviour in `useTimeline` is unchanged
(empty filter → `'timeline'` key, keeping the SSE/live patcher targeting Home's feed).

**Why this shape.** The pure module is the only thing A6 needs and the only thing with real
logic, so it carries the tests; the component is a thin PrimeVue shell (happy-dom-hostile, so
minimally tested via its emitted MemoFilter, not asserted pixel-by-pixel). Reusing `MemoQuery`
and the generated `Visibility` enum means no new API contract and no api-drift risk.

## Rejected Alternatives

- **Put the serialization inside SearchView.vue.** Violates the house rule (logic must be a pure,
  directly-tested function) and would force A6 to duplicate or import view internals. Rejected.
- **Invent a compact custom filter string now (for A6).** A6 needs *a* stable string; the URL
  `URLSearchParams` form already is one, is human-readable, and doubles as the linkable address.
  A bespoke grammar is the CEL-rebuild the roadmap explicitly forbids. Rejected.
- **Add a `has:media` / media-type chip.** No backend param exists; it would require a backend
  change, pulling A1 out of frontend-only scope. Deferred (not in A1).
- **A Pinia store for filter state.** Project forbids Pinia; filter state is local to a view +
  the URL. Rejected.
- **Client-side filtering of an unfiltered feed.** Breaks cursor pagination and correctness at
  scale; the backend already filters. Rejected.

## Phased Breakdown

### Phase 1 — Pure serialization module (`lib/memoFilter.ts`) + tests
- **Branch:** `feature/a1-search-filters` (squash-merge to main).
- **Build:** `MemoFilter` interface; `emptyFilter`, `isEmptyFilter`, `filterToQuery`,
  `filterToSearchParams`, `filterFromSearchParams`. Import `Visibility` from generated types.
- **Do NOT:** touch any `.vue` file, any backend file, `schema.d.ts`, or add a dependency.
- **Verification:** vitest `lib/__tests__/memoFilter.spec.ts` with a case PER category:
  empty↔empty round-trip; text-only; single tag; multiple tags; each visibility value;
  dateFrom only (→ `created_after` = local-day-start, exact ISO asserted); dateTo only
  (→ `created_before` = start of next local day, exact ISO asserted); both dates; boundary
  convention cases asserting the documented semantics — a memo at exactly the START day's local
  midnight is EXCLUDED (strict `>` knife-edge), a memo one microsecond later is INCLUDED, a memo
  at the END day's `23:59:59.999999` is INCLUDED, and a memo at the previous day's final
  microsecond is EXCLUDED (proves zero spillover); archived exclude/include/only → correct
  `include_archived`/`archived_only`;
  tags round-trip as repeated `tag=` keys (a tag containing a comma survives); `route.query`-shaped
  (`LocationQuery`) input parses identically to `URLSearchParams`; malformed/empty URL params
  ignored (no empty-string params emitted). Round-trip property:
  `filterFromSearchParams(filterToSearchParams(f))` deep-equals `f` for representative filters. Realness: deleting a mapping line turns a specific case red.
  `cd frontend && npm run test` green; `npm run typecheck` clean.
- **Status:** pending

### Phase 2 — FilterBar.vue component
- **Branch:** `feature/a1-search-filters` (continued).
- **Build:** `components/FilterBar.vue` (v-model `MemoFilter`) with the PrimeVue controls above,
  tag options from `useTags()`, debounced free-text, removable active-filter pills, Clear button.
- **Do NOT:** embed serialization or `/api/memos` calls in the component; add a media-type chip.
- **Verification:** a focused vitest asserting the extracted glue (e.g. a pure
  `pillsFor(filter)` helper and the debounce/clear emit) rather than PrimeVue internals in
  happy-dom; `npm run test` + `npm run build` green.
- **Status:** pending

### Phase 3 — Wire SearchView, unify HomeView, URL sync
- **Branch:** `feature/a1-search-filters` (continued).
- **Build:** SearchView renders FilterBar + feed on non-empty filter, `filterToQuery` → MemoFeed,
  URL `router.replace`. HomeView replaces its `?tag=` block with `filterFromSearchParams`.
- **Do NOT:** change `useTimeline`'s query-key logic or ArchivedView's behaviour; regress the
  300 ms free-text debounce; alter the SSE/live-patch `'timeline'` key path.
- **Verification:** update/keep `SearchView.spec.ts` (+ any HomeView spec) green; full frontend
  gate `npm run test && npm run build && npm run typecheck`; **backend/api-drift unchanged** —
  confirm `git diff --stat` touches no `app/`, no `openapi.json`, no `schema.d.ts`; manual
  smoke: a tag+date filter narrows the feed and the URL reflects it, reload restores it.
- **Status:** pending

## Execution Protocol (Agentic Review Loop)

Per phase, three gates in order:

1. **Pre-execution gate (before code):** Codex reviews the phase's Build/Do-NOT/Verification
   against the branch state — `codex exec --sandbox read-only - < prompt.txt` (stdin redirected),
   independent analysis first, typed verdict (`VERDICT: APPROVE | REVISE` + file:line) in one
   fenced block at the end; parse the LAST occurrence; delta rounds via
   `codex exec --sandbox read-only resume <session-id> - < delta.txt`.
2. **Implementation:** on `feature/a1-search-filters`, honoring Do-NOT cuts; phase verification
   commands pass locally before review. If reality contradicts the spec, stop and update the plan.
3. **Post-phase code gate:** Codex reviews the actual diff read-only (or the
   `agent-review-orchestrator.py` path); blocking verdicts cite file:line.

Before merge: final cross-model audit of the whole branch against Acceptance Criteria, plus the
standard release gates (frontend build, backend+api-drift unchanged, CI green all jobs). Invoke
`/disciplined-implementation` before Phase 1 and carry it through. This is a frontend-only,
no-dependency change, so no slim-image build is required (documented, not skipped).

## Dependencies

- None blocking. Reuses existing `useTimeline` (`MemoQuery`), `useTags`, `MemoFeed`, the
  generated `Visibility` type, `router.ts`, and PrimeVue 4 Aura (already installed).
- A6 (saved shortcuts) DEPENDS ON this plan's URL serialization — do not change its shape after
  A6 lands without migrating stored shortcuts.

## Dependencies on Current State

- `frontend/src/composables/useTimeline.ts` (`MemoQuery`, `useTimeline`), `useTags.ts`.
- `frontend/src/views/SearchView.vue` (:1-39), `frontend/src/views/HomeView.vue` (`?tag=` block),
  `components/MemoFeed.vue` (`:query` prop), `router.ts`.
- `app/routers/memos.py:180-215` — the filter params (READ-ONLY reference; not modified).
- Generated `frontend/src/api/schema.d.ts` — `Visibility` enum (imported, not regenerated).

## Risks

- **Date boundary bugs** (strict `<`/`>` vs a day picker, timezone, empty→422). Mitigation: a
  documented day-granularity convention — dateTo→next-local-day-start (upper bound exact),
  dateFrom→local-day-start (no epsilon → zero previous-day spillover; the exact-midnight tie is
  excluded by design) — with the boundary cases (start-midnight excluded, +1µs included,
  end-of-day included, prior-day-final-µs excluded) and drop-empty all pinned by dedicated
  per-category unit tests in Phase 1. This day-granularity convention is what A6 persists; it is
  explicitly NOT exact set-theoretic inclusivity (which strict `>` cannot express).
- **A6 coupling** — a later serialization change would break stored shortcuts. Mitigation: the
  round-trip property test locks the shape; documented as a contract in Dependencies.
- **Accidental api-drift** if a stray backend/type edit sneaks in. Mitigation: Phase 3
  verification asserts `git diff` touches no `app/`/`openapi.json`/`schema.d.ts`.
- **SSE live-patch regression** if the `'timeline'` query-key path changes. Mitigation: explicit
  Do-NOT + keep Home's empty-filter key.
- **happy-dom limits** — over-testing PrimeVue internals gives false failures. Mitigation: test
  the pure module + extracted helpers, not the widget DOM.

## Acceptance Criteria

1. `lib/memoFilter.ts` exists with the pure functions; vitest covers every input category listed
   in Phase 1 and the round-trip property; removing a mapping turns a test red.
2. SearchView shows a filter bar; filtering by tag(s), visibility, and a date range narrows the
   feed via `GET /api/memos` (no client-side filtering).
3. The active filter is reflected in the URL and restored on reload; a sidebar tag click on Home
   still filters and now composes with other filters, using the shared module.
4. `git diff main...HEAD` touches NO `app/` file, `openapi.json`, or `schema.d.ts`
   (api-drift/backend jobs green by construction).
5. Frontend gates green: `npm run test`, `npm run build`, `npm run typecheck`; CI green on ALL
   jobs on the PR commit.
6. The URL serialization is documented as the exact form A6 will persist.
