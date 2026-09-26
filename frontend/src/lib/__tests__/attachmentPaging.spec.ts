import { describe, expect, it } from 'vitest'

import { appendPage, nextCursorFrom, shouldLoadMore } from '@/lib/attachmentPaging'

describe('nextCursorFrom', () => {
  it('reads the continuation token the list endpoint sends', () => {
    expect(nextCursorFrom(new Headers({ 'X-Next-Cursor': 'abc123' }))).toBe('abc123')
  })
  it('reads it case-insensitively, as HTTP header names are', () => {
    expect(nextCursorFrom(new Headers({ 'x-next-cursor': 'abc123' }))).toBe('abc123')
  })
  it('ends paging when the header is absent (the last page)', () => {
    expect(nextCursorFrom(new Headers())).toBeUndefined()
  })
  it('ends paging on an empty or blank header rather than resending it', () => {
    expect(nextCursorFrom(new Headers({ 'X-Next-Cursor': '' }))).toBeUndefined()
    expect(nextCursorFrom(new Headers({ 'X-Next-Cursor': '   ' }))).toBeUndefined()
  })
  it('ends paging when there is no response to read', () => {
    expect(nextCursorFrom(undefined)).toBeUndefined()
    expect(nextCursorFrom(null)).toBeUndefined()
  })
})

describe('appendPage', () => {
  const a = { uid: 'a' }
  const b = { uid: 'b' }
  const c = { uid: 'c' }
  it('is empty before any page arrives', () => {
    expect(appendPage([])).toEqual([])
  })
  it('keeps one page as it came', () => {
    expect(appendPage([[a, b]])).toEqual([a, b])
  })
  it('appends later pages after earlier ones, in page order', () => {
    expect(appendPage([[a, b], [c]])).toEqual([a, b, c])
  })
  it('skips an empty page in the middle', () => {
    expect(appendPage([[a], [], [c]])).toEqual([a, c])
  })
  it('keeps the first copy of a uid two pages both carry', () => {
    // Tiles are keyed by uid: a repeated one would collide in the gallery.
    const bAgain = { uid: 'b', stale: true }
    expect(appendPage([[a, b], [bAgain, c]])).toEqual([a, b, c])
  })
})

describe('shouldLoadMore', () => {
  const ready = { visible: true, hasNextPage: true, isFetchingNextPage: false, isError: false }
  it('loads the next page when the sentinel is on screen and one exists', () => {
    expect(shouldLoadMore(ready)).toBe(true)
  })
  it('waits while the sentinel is off screen', () => {
    expect(shouldLoadMore({ ...ready, visible: false })).toBe(false)
  })
  it('stops after the last page', () => {
    expect(shouldLoadMore({ ...ready, hasNextPage: false })).toBe(false)
  })
  it('never starts a second request while one is in flight', () => {
    expect(shouldLoadMore({ ...ready, isFetchingNextPage: true })).toBe(false)
  })
  it('does not retry a failed page on its own (the user retries)', () => {
    expect(shouldLoadMore({ ...ready, isError: true })).toBe(false)
  })
})
