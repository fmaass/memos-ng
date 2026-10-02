import { describe, expect, it } from 'vitest'

import {
  clearedLibraryFilter,
  defaultLibraryFilter,
  fromSearchParams,
  libraryEmptyState,
  type LibraryFilter,
  toApiParams,
  toBucketParams,
  toRouteQuery,
  toSearchParams,
  viewFromQuery,
  viewToQuery,
  withView,
} from '@/lib/libraryFilter'

describe('libraryFilter', () => {
  it('reads every field from the URL', () => {
    expect(fromSearchParams(new URLSearchParams('type=video&order=oldest&tag=recap'))).toEqual({
      type: 'video',
      order: 'oldest',
      tag: 'recap',
    })
  })

  it('defaults to newest first, every kind, every tag, on an empty URL', () => {
    expect(fromSearchParams(new URLSearchParams(''))).toEqual({ order: 'newest' })
    expect(defaultLibraryFilter()).toEqual({ order: 'newest' })
  })

  it.each<[string, LibraryFilter]>([
    ['', { order: 'newest' }],
    ['type=image', { type: 'image', order: 'newest' }],
    ['type=video&order=oldest', { type: 'video', order: 'oldest' }],
    ['order=oldest&tag=recap', { order: 'oldest', tag: 'recap' }],
    ['type=video&order=oldest&tag=a%26b%20c', { type: 'video', order: 'oldest', tag: 'a&b c' }],
  ])('round-trips %j through URLSearchParams', (_qs, filter) => {
    const wire = new URLSearchParams(toSearchParams(filter).toString())
    expect(fromSearchParams(wire)).toEqual(filter)
  })

  it('round-trips through the route query form', () => {
    const filter: LibraryFilter = { type: 'image', order: 'oldest', tag: 'recap' }
    expect(fromSearchParams(toRouteQuery(filter))).toEqual(filter)
  })

  it('leaves the defaults out of the URL, so the bare route IS the default filter', () => {
    expect(toRouteQuery({ order: 'newest' })).toEqual({})
    expect(toSearchParams({ order: 'newest' }).toString()).toBe('')
  })

  it.each([
    ['type=audio', { order: 'newest' }],
    ['type=all', { order: 'newest' }],
    ['type=VIDEO', { order: 'newest' }],
    ['order=taken_asc', { order: 'newest' }],
    ['order=', { order: 'newest' }],
    ['tag=', { order: 'newest' }],
    ['type=gif&order=sideways&tag=recap', { order: 'newest', tag: 'recap' }],
  ])('rejects the unknown value in %j and falls back to the default', (qs, expected) => {
    expect(fromSearchParams(new URLSearchParams(qs))).toEqual(expected)
  })

  it('accepts a vue-router LocationQuery, arrays and nulls included', () => {
    expect(fromSearchParams({ type: ['video', 'image'], order: null, tag: 'recap' })).toEqual({
      type: 'video',
      order: 'newest',
      tag: 'recap',
    })
  })

  it('maps onto the list API: taken order, and only the fields that are set', () => {
    expect(toApiParams({ type: 'video', order: 'oldest', tag: 'recap' })).toEqual({
      order: 'taken_asc',
      type: 'video',
      tag: 'recap',
    })
    expect(toApiParams({ order: 'newest' })).toEqual({ order: 'taken_desc' })
  })
})

describe('the Library view beside the filter', () => {
  it.each([
    [{ view: 'years' }, 'years'],
    [{ view: 'months' }, 'months'],
    [{ view: 'all' }, 'all'],
    [{}, 'all'],
    [{ view: 'grid' }, 'all'],
    [{ view: ['months', 'years'] }, 'months'],
  ] as [Record<string, string | string[]>, string][])('reads %j as %s', (query, view) => {
    expect(viewFromQuery(query)).toBe(view)
  })

  it('leaves All, the default, out of the URL like the filter’s defaults', () => {
    expect(viewToQuery('all')).toEqual({})
    expect(viewToQuery('months')).toEqual({ view: 'months' })
  })

  it('changes only the view and keeps every filter field', () => {
    const query = { type: 'video', order: 'oldest', tag: 'recap', view: 'years' }
    expect(withView(query, 'months')).toEqual({ type: 'video', order: 'oldest', tag: 'recap', view: 'months' })
    expect(withView(query, 'all')).toEqual({ type: 'video', order: 'oldest', tag: 'recap' })
  })

  it('never puts the view inside the filter record', () => {
    expect(fromSearchParams({ view: 'months', type: 'video' })).toEqual({ order: 'newest', type: 'video' })
  })
})

describe('toBucketParams', () => {
  it('names size, the Zurich zone, type and tag — and never order, which the buckets endpoint does not take', () => {
    const f: LibraryFilter = { type: 'video', order: 'oldest', tag: 'recap' }
    expect(toBucketParams('day', f)).toEqual({ size: 'day', tz: 'Europe/Zurich', type: 'video', tag: 'recap' })
  })

  it('leaves out the fields the filter does not set', () => {
    expect(toBucketParams('year', defaultLibraryFilter())).toEqual({ size: 'year', tz: 'Europe/Zurich' })
  })
})

describe('libraryEmptyState', () => {
  it('keeps the plain copy for the whole Library', () => {
    expect(libraryEmptyState(defaultLibraryFilter())).toEqual({ message: 'No attachments yet.', filtered: false })
  })

  it('treats an order-only filter as the whole Library', () => {
    expect(libraryEmptyState({ order: 'oldest' })).toEqual({ message: 'No attachments yet.', filtered: false })
  })

  it('treats an empty tag as no tag', () => {
    expect(libraryEmptyState({ order: 'newest', tag: '' })).toEqual({ message: 'No attachments yet.', filtered: false })
  })

  it('names the tag of a tag-only filter', () => {
    expect(libraryEmptyState({ order: 'newest', tag: 'no-such-tag-zz' })).toEqual({
      message: 'No pictures tagged #no-such-tag-zz.',
      filtered: true,
    })
  })

  it('names the kind of a kind-only filter, for each kind', () => {
    expect(libraryEmptyState({ order: 'newest', type: 'image' })).toEqual({
      message: 'No photos match this filter.',
      filtered: true,
    })
    expect(libraryEmptyState({ order: 'oldest', type: 'video' })).toEqual({
      message: 'No videos match this filter.',
      filtered: true,
    })
  })

  it('names both the kind and the tag of a combined filter', () => {
    expect(libraryEmptyState({ order: 'newest', type: 'video', tag: 'recap' })).toEqual({
      message: 'No videos tagged #recap.',
      filtered: true,
    })
  })
})

describe('clearedLibraryFilter', () => {
  it('drops the kind and the tag and keeps the order', () => {
    expect(clearedLibraryFilter({ order: 'oldest', type: 'video', tag: 'recap' })).toEqual({ order: 'oldest' })
    expect(clearedLibraryFilter({ order: 'newest', tag: 'x' })).toEqual(defaultLibraryFilter())
  })
})
