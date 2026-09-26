import type { operations } from '@/api/schema'

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
