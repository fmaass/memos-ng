import type { operations } from '@/api/schema'
import { LIBRARY_TIME_ZONE } from '@/lib/librarySections'

// The Library's filter as the UI holds it, and the ONE place its three forms are derived: the
// address bar (linkable, reload-stable), the route query (for router.replace) and the list API's
// params. Defaults are left out of the URL, so the bare /resources IS the default filter and two
// equal filters always serialize identically (the loop guard of the two-way route sync).
export type LibraryKind = 'image' | 'video'
export type LibraryOrder = 'newest' | 'oldest'

export interface LibraryFilter {
  type?: LibraryKind
  order: LibraryOrder
  tag?: string
}

type ListQuery = NonNullable<operations['list_attachments_api_attachments_get']['parameters']['query']>
export type LibraryApiParams = Required<Pick<ListQuery, 'order'>> & {
  type?: LibraryKind
  tag?: string
}

// Vue Router's LocationQuery, accepted as well so a view can hand `route.query` straight in.
type LocationQueryLike = Record<string, string | (string | null)[] | null | undefined>

const KINDS: readonly LibraryKind[] = ['image', 'video']
const ORDERS: readonly LibraryOrder[] = ['newest', 'oldest']

export function defaultLibraryFilter(): LibraryFilter {
  return { order: 'newest' }
}

// Every field is single-valued: a repeated key keeps its first value, as URLSearchParams.get does.
function first(input: URLSearchParams | LocationQueryLike, key: string): string | null {
  if (input instanceof URLSearchParams) return input.get(key)
  const v = input[key]
  if (Array.isArray(v)) return v.find((item) => item != null) ?? null
  return v ?? null
}

// A value the Library does not know (a hand-edited or stale link) falls back to that field's
// default instead of reaching the server as a parameter it would refuse.
export function fromSearchParams(input: URLSearchParams | LocationQueryLike): LibraryFilter {
  const f = defaultLibraryFilter()
  const type = first(input, 'type')
  if (KINDS.includes(type as LibraryKind)) f.type = type as LibraryKind
  const order = first(input, 'order')
  if (ORDERS.includes(order as LibraryOrder)) f.order = order as LibraryOrder
  const tag = first(input, 'tag')
  if (tag) f.tag = tag
  return f
}

export function toSearchParams(f: LibraryFilter): URLSearchParams {
  const sp = new URLSearchParams()
  if (f.type) sp.set('type', f.type)
  if (f.order !== 'newest') sp.set('order', f.order)
  if (f.tag) sp.set('tag', f.tag)
  return sp
}

export function toRouteQuery(f: LibraryFilter): Record<string, string> {
  return Object.fromEntries(toSearchParams(f))
}

// The Library always walks the list by takenAt; `order` only picks the direction.
export function toApiParams(f: LibraryFilter): LibraryApiParams {
  return {
    order: f.order === 'oldest' ? 'taken_asc' : 'taken_desc',
    ...(f.type ? { type: f.type } : {}),
    ...(f.tag ? { tag: f.tag } : {}),
  }
}

export function sameLibraryFilter(a: LibraryFilter, b: LibraryFilter): boolean {
  return toSearchParams(a).toString() === toSearchParams(b).toString()
}

// Years | Months | All lives in the route BESIDE the filter, never inside it: the list's and the
// buckets' query keys are built from the filter record, so a segment change must not reach them.
// Like the filter's defaults, All is left out of the URL.
export type LibraryView = 'years' | 'months' | 'all'
const VIEWS: readonly LibraryView[] = ['years', 'months', 'all']

export function viewFromQuery(input: URLSearchParams | LocationQueryLike): LibraryView {
  const view = first(input, 'view')
  return VIEWS.includes(view as LibraryView) ? (view as LibraryView) : 'all'
}

export function viewToQuery(view: LibraryView): Record<string, string> {
  return view === 'all' ? {} : { view }
}

// Every other field of the query is kept as it is, so a segment change never drops the filter.
export function withView(input: LocationQueryLike, view: LibraryView): LocationQueryLike {
  const rest = { ...input }
  delete rest.view
  return { ...rest, ...viewToQuery(view) }
}

// GET /api/attachments/buckets takes size, tz, type and tag and NO order, so its params are named
// one by one here rather than taken from toApiParams, which always carries an order.
export type BucketSize = 'day' | 'month' | 'year'
type BucketQuery = operations['attachment_buckets_api_attachments_buckets_get']['parameters']['query']

export function toBucketParams(size: BucketSize, f: LibraryFilter): BucketQuery {
  return {
    size,
    tz: LIBRARY_TIME_ZONE,
    ...(f.type ? { type: f.type } : {}),
    ...(f.tag ? { tag: f.tag } : {}),
  }
}

// What an empty Library says. `order` narrows nothing, so an order-only filter is still the whole
// Library; a tag or a kind is a restriction, and then the copy names it and the view offers a way out.
export interface LibraryEmptyState {
  message: string
  filtered: boolean
}

const KIND_NOUN: Record<LibraryKind, string> = { image: 'photos', video: 'videos' }

export function libraryEmptyState(f: LibraryFilter): LibraryEmptyState {
  if (!f.type && !f.tag) return { message: 'No attachments yet.', filtered: false }
  const noun = f.type ? KIND_NOUN[f.type] : 'pictures'
  const what = f.tag ? `tagged #${f.tag}` : 'match this filter'
  return { message: `No ${noun} ${what}.`, filtered: true }
}

// Clearing drops what restricts the result and keeps the direction the viewer chose.
export function clearedLibraryFilter(f: LibraryFilter): LibraryFilter {
  return { order: f.order }
}
