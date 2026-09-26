// Paging the media library (GET /api/attachments): the body is a bare array, and the continuation
// token travels in the X-Next-Cursor header, absent on the last page. Pure so the paging decisions
// are unit-tested without a DOM (happy-dom has no IntersectionObserver).

export const NEXT_CURSOR_HEADER = 'X-Next-Cursor'

// A blank header would be resent as `?cursor=` and loop on the first page, so it ends paging too.
export function nextCursorFrom(headers: Headers | null | undefined): string | undefined {
  const cursor = headers?.get(NEXT_CURSOR_HEADER)?.trim()
  return cursor ? cursor : undefined
}

// Tiles are keyed by uid, so a uid two pages both carry (a row that moved between two reads) keeps
// its first position instead of colliding in the gallery.
export function appendPage<T extends { uid: string }>(pages: readonly (readonly T[])[]): T[] {
  const seen = new Set<string>()
  const out: T[] = []
  for (const page of pages) {
    for (const item of page) {
      if (seen.has(item.uid)) continue
      seen.add(item.uid)
      out.push(item)
    }
  }
  return out
}

export interface LoadMoreState {
  visible: boolean
  hasNextPage: boolean
  isFetchingNextPage: boolean
  isError: boolean
}

// The sentinel's observer fires on a CHANGE of intersection only: a first page shorter than the
// screen leaves it visible, and nothing would ever fire again. So the decision is re-taken whenever
// any input changes, and a failed page waits for the user's retry instead of looping.
export function shouldLoadMore(s: LoadMoreState): boolean {
  return s.visible && s.hasNextPage && !s.isFetchingNextPage && !s.isError
}
