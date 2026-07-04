import { describe, expect, it } from 'vitest'

import {
  emptyFilter,
  filterFromSearchParams,
  filterToQuery,
  filterToSearchParams,
  isEmptyFilter,
  type MemoFilter,
} from '@/lib/memoFilter'

// The day-granularity date convention (documented in the A1 plan): the backend compares
// created_at with STRICT `<`/`>`. So dateTo=D -> created_before = start of local day D+1
// (all of day D included), and dateFrom=D -> created_after = start of local day D (no epsilon;
// a memo at exactly local midnight of the start day is EXCLUDED; zero previous-day spillover).
// These helpers build the expected local-day-boundary ISO instants the same way the module must.
function localDayStartISO(y: number, m: number, d: number): string {
  return new Date(y, m - 1, d, 0, 0, 0, 0).toISOString()
}

describe('emptyFilter / isEmptyFilter', () => {
  it('emptyFilter has no active fields', () => {
    const f = emptyFilter()
    expect(f.tags).toEqual([])
    expect(isEmptyFilter(f)).toBe(true)
  })

  it('any populated field makes the filter non-empty', () => {
    expect(isEmptyFilter({ ...emptyFilter(), text: 'hi' })).toBe(false)
    expect(isEmptyFilter({ ...emptyFilter(), tags: ['work'] })).toBe(false)
    expect(isEmptyFilter({ ...emptyFilter(), visibility: 'PUBLIC' })).toBe(false)
    expect(isEmptyFilter({ ...emptyFilter(), dateFrom: '2026-07-04' })).toBe(false)
    expect(isEmptyFilter({ ...emptyFilter(), dateTo: '2026-07-04' })).toBe(false)
    expect(isEmptyFilter({ ...emptyFilter(), archived: 'only' })).toBe(false)
    // 'exclude' is the default archived mode, so it does NOT count as active
    expect(isEmptyFilter({ ...emptyFilter(), archived: 'exclude' })).toBe(true)
  })
})

describe('filterToQuery', () => {
  it('empty filter -> empty query (no stray params)', () => {
    expect(filterToQuery(emptyFilter())).toEqual({})
  })

  it('text-only -> q', () => {
    expect(filterToQuery({ ...emptyFilter(), text: 'beach' })).toEqual({ q: 'beach' })
  })

  it('single tag -> tag array', () => {
    expect(filterToQuery({ ...emptyFilter(), tags: ['work'] })).toEqual({ tag: ['work'] })
  })

  it('multiple tags -> tag array preserves all', () => {
    expect(filterToQuery({ ...emptyFilter(), tags: ['work', 'home'] })).toEqual({
      tag: ['work', 'home'],
    })
  })

  it('each visibility value maps through', () => {
    for (const v of ['PRIVATE', 'PROTECTED', 'PUBLIC'] as const) {
      expect(filterToQuery({ ...emptyFilter(), visibility: v })).toEqual({ visibility: v })
    }
  })

  it('dateFrom -> created_after at start of local day (no epsilon)', () => {
    expect(filterToQuery({ ...emptyFilter(), dateFrom: '2026-07-04' })).toEqual({
      created_after: localDayStartISO(2026, 7, 4),
    })
  })

  it('dateTo -> created_before at start of NEXT local day', () => {
    expect(filterToQuery({ ...emptyFilter(), dateTo: '2026-07-04' })).toEqual({
      created_before: localDayStartISO(2026, 7, 5),
    })
  })

  it('both dates -> both bounds', () => {
    expect(filterToQuery({ ...emptyFilter(), dateFrom: '2026-07-01', dateTo: '2026-07-04' })).toEqual(
      {
        created_after: localDayStartISO(2026, 7, 1),
        created_before: localDayStartISO(2026, 7, 5),
      },
    )
  })

  it('archived exclude -> no archived params (default)', () => {
    expect(filterToQuery({ ...emptyFilter(), archived: 'exclude' })).toEqual({})
  })

  it('archived include -> include_archived', () => {
    expect(filterToQuery({ ...emptyFilter(), archived: 'include' })).toEqual({
      include_archived: true,
    })
  })

  it('archived only -> archived_only', () => {
    expect(filterToQuery({ ...emptyFilter(), archived: 'only' })).toEqual({ archived_only: true })
  })
})

// Boundary convention: prove the documented day-granularity semantics against the strict
// backend comparison operators. created_after is start-of-day (strict `>` excludes exact
// midnight); created_before is next-day-start (strict `<` includes all of the end day).
describe('date boundary convention (strict-comparison faithful)', () => {
  const q = filterToQuery({ ...emptyFilter(), dateFrom: '2026-07-04', dateTo: '2026-07-04' })
  const after = new Date(q.created_after as string)
  const before = new Date(q.created_before as string)

  it('a memo at exactly the start day local midnight is EXCLUDED (strict >)', () => {
    const midnight = new Date(2026, 6, 4, 0, 0, 0, 0)
    expect(midnight > after).toBe(false) // not strictly after -> excluded
  })

  it('a memo one ms after start midnight is INCLUDED', () => {
    const justAfter = new Date(2026, 6, 4, 0, 0, 0, 1)
    expect(justAfter > after && justAfter < before).toBe(true)
  })

  it('a memo at the end-day 23:59:59.999 is INCLUDED', () => {
    const endOfDay = new Date(2026, 6, 4, 23, 59, 59, 999)
    expect(endOfDay > after && endOfDay < before).toBe(true)
  })

  it('a memo at the previous day final ms is EXCLUDED (no spillover)', () => {
    const prevDayEnd = new Date(2026, 6, 3, 23, 59, 59, 999)
    expect(prevDayEnd > after).toBe(false)
  })
})

describe('URL round-trip (the A6 serialization contract)', () => {
  it('tags serialize as REPEATED tag keys, not comma-joined', () => {
    const sp = filterToSearchParams({ ...emptyFilter(), tags: ['work', 'home'] })
    expect(sp.getAll('tag')).toEqual(['work', 'home'])
    expect(sp.toString()).not.toContain('work%2Chome') // no comma-joining
  })

  it('a tag containing a comma round-trips losslessly', () => {
    const f: MemoFilter = { ...emptyFilter(), tags: ['a,b'] }
    expect(filterFromSearchParams(filterToSearchParams(f))).toEqual(f)
  })

  it('round-trips a representative filter through URLSearchParams', () => {
    const f: MemoFilter = {
      text: 'hello',
      tags: ['x', 'y'],
      visibility: 'PROTECTED',
      dateFrom: '2026-07-01',
      dateTo: '2026-07-04',
      archived: 'include',
    }
    expect(filterFromSearchParams(filterToSearchParams(f))).toEqual(f)
  })

  it('parses a Vue Router LocationQuery (route.query) identically to URLSearchParams', () => {
    const f: MemoFilter = { ...emptyFilter(), tags: ['work', 'home'], visibility: 'PUBLIC' }
    const sp = filterToSearchParams(f)
    // LocationQuery shape: repeated keys become arrays; single keys are strings; nulls possible.
    const locationQuery: Record<string, string | string[] | null> = {
      tag: ['work', 'home'],
      visibility: 'PUBLIC',
    }
    expect(filterFromSearchParams(locationQuery)).toEqual(filterFromSearchParams(sp))
    expect(filterFromSearchParams(locationQuery)).toEqual(f)
  })

  it('ignores empty/malformed params and never emits empty-string values', () => {
    const sp = filterToSearchParams({ ...emptyFilter(), text: '', tags: [] })
    expect(sp.toString()).toBe('')
    expect(filterFromSearchParams(new URLSearchParams('q=&tag='))).toEqual(emptyFilter())
  })
})
