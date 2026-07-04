import type { components } from '@/api/schema'
import type { MemoQuery } from '@/composables/useTimeline'

// The UI-facing filter state. `dateFrom`/`dateTo` are LOCAL calendar dates ('YYYY-MM-DD' from the
// DatePicker); `archived` defaults to 'exclude'. This object (via its URL form) is the exact
// serialization A6 (saved shortcuts) persists — keep it stable.
export type Visibility = components['schemas']['Visibility']

export interface MemoFilter {
  text?: string
  tags: string[]
  visibility?: Visibility
  dateFrom?: string
  dateTo?: string
  archived?: 'exclude' | 'include' | 'only'
}

export function emptyFilter(): MemoFilter {
  return { tags: [] }
}

export function isEmptyFilter(f: MemoFilter): boolean {
  return (
    !f.text &&
    (f.tags?.length ?? 0) === 0 &&
    !f.visibility &&
    !f.dateFrom &&
    !f.dateTo &&
    (!f.archived || f.archived === 'exclude')
  )
}

// Active-filter pills: one descriptor per active field (one per tag), each with a stable `key`
// that removeFilterKey understands. 'exclude' archived is the default and shows no pill.
export interface FilterPill {
  key: string
  label: string
}

const VISIBILITY_LABEL: Record<Visibility, string> = {
  PRIVATE: 'Private',
  PROTECTED: 'Protected',
  PUBLIC: 'Public',
}

export function pillsFor(f: MemoFilter): FilterPill[] {
  const pills: FilterPill[] = []
  if (f.text) pills.push({ key: 'text', label: `"${f.text}"` })
  for (const t of f.tags ?? []) pills.push({ key: `tag:${t}`, label: `#${t}` })
  if (f.visibility) pills.push({ key: 'visibility', label: VISIBILITY_LABEL[f.visibility] })
  if (f.dateFrom) pills.push({ key: 'dateFrom', label: `From ${f.dateFrom}` })
  if (f.dateTo) pills.push({ key: 'dateTo', label: `To ${f.dateTo}` })
  if (f.archived === 'include') pills.push({ key: 'archived', label: 'Incl. archived' })
  else if (f.archived === 'only') pills.push({ key: 'archived', label: 'Archived only' })
  return pills
}

// Return a new filter (never mutating the input) with the piece named by `key` cleared. A
// 'tag:<name>' key removes only that one tag; 'archived' resets to the default 'exclude'.
export function removeFilterKey(f: MemoFilter, key: string): MemoFilter {
  const next: MemoFilter = { ...f, tags: [...(f.tags ?? [])] }
  if (key.startsWith('tag:')) {
    const name = key.slice(4)
    next.tags = next.tags.filter((t) => t !== name)
  } else if (key === 'text') next.text = undefined
  else if (key === 'visibility') next.visibility = undefined
  else if (key === 'dateFrom') next.dateFrom = undefined
  else if (key === 'dateTo') next.dateTo = undefined
  // undefined is the canonical "default" (≡ 'exclude'); keeps a cleared filter deep-equal to
  // emptyFilter() and round-trip-stable. The FilterBar maps undefined→'exclude' for display.
  else if (key === 'archived') next.archived = undefined
  return next
}

// A local 'YYYY-MM-DD' at 00:00:00.000 local time, offset by `addDays` (JS normalizes overflow,
// so month/year rollover and DST are handled). Returns null for a malformed date string.
function localMidnight(dateStr: string, addDays = 0): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr)
  if (!m) return null
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]) + addDays, 0, 0, 0, 0)
}

// Map the UI filter onto the /api/memos query params. Day-granularity date convention against the
// backend's STRICT comparisons (created_at < created_before, created_at > created_after):
// dateTo=D -> created_before = start of local day D+1 (all of D included); dateFrom=D ->
// created_after = start of local day D (no epsilon: exact-midnight-start excluded, zero spillover).
export function filterToQuery(f: MemoFilter): MemoQuery {
  const q: MemoQuery = {}
  if (f.text) q.q = f.text
  if (f.tags?.length) q.tag = [...f.tags]
  if (f.visibility) q.visibility = f.visibility
  if (f.dateFrom) {
    const start = localMidnight(f.dateFrom)
    if (start) q.created_after = start.toISOString()
  }
  if (f.dateTo) {
    const nextDay = localMidnight(f.dateTo, 1)
    if (nextDay) q.created_before = nextDay.toISOString()
  }
  if (f.archived === 'include') q.include_archived = true
  else if (f.archived === 'only') q.archived_only = true
  return q
}

// URL form (the A6 persisted contract): human-readable calendar dates + REPEATED `tag` keys
// (matching the backend's repeatable list[str] param), so it round-trips losslessly and doubles
// as the linkable address-bar query.
export function filterToSearchParams(f: MemoFilter): URLSearchParams {
  const sp = new URLSearchParams()
  if (f.text) sp.set('q', f.text)
  for (const t of f.tags ?? []) sp.append('tag', t)
  if (f.visibility) sp.set('visibility', f.visibility)
  if (f.dateFrom) sp.set('dateFrom', f.dateFrom)
  if (f.dateTo) sp.set('dateTo', f.dateTo)
  if (f.archived && f.archived !== 'exclude') sp.set('archived', f.archived)
  return sp
}

// Vue Router's `route.query` is a LocationQuery (Record<string, string | (string|null)[] | null>),
// NOT a URLSearchParams — accept both so views can pass `route.query` straight in.
type LocationQueryLike = Record<string, string | (string | null)[] | null>

function toParams(input: URLSearchParams | LocationQueryLike): URLSearchParams {
  if (input instanceof URLSearchParams) return input
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(input)) {
    if (v == null) continue
    if (Array.isArray(v)) {
      for (const item of v) if (item != null) sp.append(k, item)
    } else {
      sp.append(k, v)
    }
  }
  return sp
}

// The Vue Router LocationQuery form (for router.push/replace): repeated keys become arrays,
// single keys stay strings — the array-based sibling of filterToSearchParams, round-tripping via
// filterFromSearchParams (which accepts LocationQuery). Use this to sync the filter into the URL.
export function filterToRouteQuery(f: MemoFilter): Record<string, string | string[]> {
  const sp = filterToSearchParams(f)
  const out: Record<string, string | string[]> = {}
  for (const key of new Set(sp.keys())) {
    const all = sp.getAll(key)
    out[key] = all.length > 1 ? all : all[0]
  }
  return out
}

export function filterFromSearchParams(input: URLSearchParams | LocationQueryLike): MemoFilter {
  const sp = toParams(input)
  const f = emptyFilter()
  const q = sp.get('q')
  if (q) f.text = q
  const tags = sp.getAll('tag').filter(Boolean)
  if (tags.length) f.tags = tags
  const vis = sp.get('visibility')
  if (vis === 'PRIVATE' || vis === 'PROTECTED' || vis === 'PUBLIC') f.visibility = vis
  const df = sp.get('dateFrom')
  if (df) f.dateFrom = df
  const dt = sp.get('dateTo')
  if (dt) f.dateTo = dt
  const arch = sp.get('archived')
  if (arch === 'include' || arch === 'only') f.archived = arch
  return f
}
