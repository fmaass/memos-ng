import { describe, expect, it } from 'vitest'

import {
  dayHeadingParts,
  dayPillLabel,
  groupByDay,
  groupByMonth,
  topDay,
} from '@/lib/librarySections'

const ZH = 'Europe/Zurich'
const at = (uid: string, takenAt: string) => ({ uid, takenAt })
const keys = (items: { uid: string; takenAt: string }[], tz?: string) =>
  groupByMonth(items, tz).map((s) => s.key)

// Every fixture sits where a UTC slice of the instant (toISOString) names a different month than
// the Zurich calendar does, or pairs such an item with the case under test — a UTC-sliced
// implementation fails every one of them.
describe('groupByMonth', () => {
  it('puts a 22:30 UTC item on the last of August into September in Zurich', () => {
    const sections = groupByMonth([at('a', '2026-08-31T22:30:00Z')], ZH)
    expect(sections).toEqual([
      { key: '2026-09', label: 'September 2026', items: [at('a', '2026-08-31T22:30:00Z')] },
    ])
  })

  it("puts New Year's Eve 23:30 UTC into January of the next year", () => {
    const sections = groupByMonth([at('a', '2025-12-31T23:30:00Z')], ZH)
    expect(sections.map((s) => [s.key, s.label])).toEqual([['2026-01', 'January 2026']])
  })

  it('keeps one March section across the spring DST change and follows the summer offset', () => {
    const sections = groupByMonth(
      [
        at('before', '2026-03-29T00:30:00Z'), // 01:30 CET, before the clocks go forward
        at('after', '2026-03-29T01:30:00Z'), // 03:30 CEST, after
        at('april', '2026-03-31T22:30:00Z'), // 00:30 CEST on 1 April: +2 now, not +1
      ],
      ZH,
    )
    expect(sections.map((s) => [s.key, s.label, s.items.map((i) => i.uid)])).toEqual([
      ['2026-03', 'March 2026', ['before', 'after']],
      ['2026-04', 'April 2026', ['april']],
    ])
  })

  it('keeps one October section across the autumn DST change and follows the winter offset', () => {
    const sections = groupByMonth(
      [
        at('dst', '2026-10-25T00:30:00Z'), // 02:30 CEST on the night the clocks go back
        at('november', '2026-10-31T23:30:00Z'), // 00:30 CET on 1 November: +1 again
      ],
      ZH,
    )
    expect(sections.map((s) => [s.key, s.label, s.items.map((i) => i.uid)])).toEqual([
      ['2026-10', 'October 2026', ['dst']],
      ['2026-11', 'November 2026', ['november']],
    ])
  })

  it('starts the month at exactly Zurich midnight, not a millisecond later', () => {
    expect(keys([at('m', '2026-06-30T22:00:00Z')], ZH)).toEqual(['2026-07'])
    expect(keys([at('m', '2026-06-30T21:59:59.999Z')], ZH)).toEqual(['2026-06'])
  })

  it('keeps the list order: sections in order of first appearance, items in list order', () => {
    const newestFirst = [
      at('c', '2026-09-02T10:00:00Z'),
      at('b', '2026-08-31T22:30:00Z'),
      at('a', '2026-08-15T10:00:00Z'),
    ]
    expect(groupByMonth(newestFirst, ZH).map((s) => [s.key, s.items.map((i) => i.uid)])).toEqual([
      ['2026-09', ['c', 'b']],
      ['2026-08', ['a']],
    ])
    expect(keys([...newestFirst].reverse(), ZH)).toEqual(['2026-08', '2026-09'])
  })

  it('defaults to Europe/Zurich and honours another zone when one is passed', () => {
    const late = [at('a', '2026-08-31T22:30:00Z')]
    expect(keys(late)).toEqual(['2026-09'])
    expect(keys(late, 'America/New_York')).toEqual(['2026-08'])
  })

  it('files an unreadable takenAt under Undated instead of throwing out of Intl', () => {
    const sections = groupByMonth([at('ok', '2026-09-02T10:00:00Z'), at('bad', 'not-a-date')], ZH)
    expect(sections.map((s) => [s.key, s.label, s.items.map((i) => i.uid)])).toEqual([
      ['2026-09', 'September 2026', ['ok']],
      ['undated', 'Undated', ['bad']],
    ])
  })

  it('returns no sections for no items', () => {
    expect(groupByMonth([], ZH)).toEqual([])
  })
})

const dayKeys = (items: { uid: string; takenAt: string }[], tz?: string) =>
  groupByDay(items, tz).map((s) => [s.key, s.items.map((i) => i.uid)])

describe('groupByDay', () => {
  it('files a 23:30 UTC item on 30 September under 1 October in Zurich', () => {
    expect(groupByDay([at('a', '2026-09-30T23:30:00Z')], ZH)).toEqual([
      { key: '2026-10-01', items: [at('a', '2026-09-30T23:30:00Z')] },
    ])
  })

  it('keeps the list order, newest first: days in order of first appearance, items in list order', () => {
    expect(
      dayKeys(
        [
          at('c', '2026-10-01T08:00:00Z'),
          at('b', '2026-09-30T23:30:00Z'), // 01:30 CEST on 1 October
          at('a', '2026-09-30T12:00:00Z'),
          at('z', '2026-09-27T09:00:00Z'),
        ],
        ZH,
      ),
    ).toEqual([
      ['2026-10-01', ['c', 'b']],
      ['2026-09-30', ['a']],
      ['2026-09-27', ['z']],
    ])
  })

  it('keeps an ascending list ascending rather than re-sorting it', () => {
    expect(
      dayKeys(
        [at('a', '2026-09-26T10:00:00Z'), at('b', '2026-09-27T10:00:00Z'), at('c', '2026-09-27T11:00:00Z')],
        ZH,
      ),
    ).toEqual([
      ['2026-09-26', ['a']],
      ['2026-09-27', ['b', 'c']],
    ])
  })

  it('starts the day at exactly Zurich midnight, not a millisecond later', () => {
    expect(dayKeys([at('m', '2026-09-26T22:00:00Z')], ZH)).toEqual([['2026-09-27', ['m']]])
    expect(dayKeys([at('m', '2026-09-26T21:59:59.999Z')], ZH)).toEqual([['2026-09-26', ['m']]])
  })

  it('defaults to Europe/Zurich and honours another zone when one is passed', () => {
    const late = [at('a', '2026-09-30T23:30:00Z')]
    expect(dayKeys(late)).toEqual([['2026-10-01', ['a']]])
    expect(dayKeys(late, 'America/New_York')).toEqual([['2026-09-30', ['a']]])
    // The zone's formatter is cached per zone: going back to Zurich must not reuse New York's.
    expect(dayKeys(late, ZH)).toEqual([['2026-10-01', ['a']]])
  })

  it('keeps an unreadable takenAt in its own group instead of dropping it or throwing', () => {
    expect(
      dayKeys([at('ok', '2026-09-27T10:00:00Z'), at('bad', 'not-a-date'), at('ok2', '2026-09-27T11:00:00Z')], ZH),
    ).toEqual([
      ['2026-09-27', ['ok', 'ok2']],
      ['undated', ['bad']],
    ])
  })

  it('returns no groups for no items', () => {
    expect(groupByDay([], ZH)).toEqual([])
  })
})

// 2026-09-26 is a Saturday and 2026-09-27 a Sunday.
describe('day labels', () => {
  it('names the heading row as weekday and day-month', () => {
    expect(dayHeadingParts('2026-09-26')).toEqual({ weekday: 'Sat', date: '26 Sep' })
    expect(dayHeadingParts('2026-09-27')).toEqual({ weekday: 'Sun', date: '27 Sep' })
  })

  it('names the scroll pill as weekday · day month year', () => {
    expect(dayPillLabel('2026-09-26')).toBe('Sat · 26 Sep 2026')
    expect(dayPillLabel('2027-01-01')).toBe('Fri · 1 Jan 2027')
  })

  it('formats the key as a calendar date, so the first and last of a year stay put', () => {
    expect(dayHeadingParts('2026-01-01')).toEqual({ weekday: 'Thu', date: '1 Jan' })
    expect(dayPillLabel('2025-12-31')).toBe('Wed · 31 Dec 2025')
  })

  it('labels the undated group Undated', () => {
    expect(dayHeadingParts('undated')).toEqual({ weekday: 'Undated', date: '' })
    expect(dayPillLabel('undated')).toBe('Undated')
  })

  it('treats a key that only contains a date, or names no real day, as undated rather than throwing', () => {
    for (const key of ['x2026-09-26', '2026-09-26T10:00', '2026-02-30', '2026-13-01', '']) {
      expect(dayHeadingParts(key)).toEqual({ weekday: 'Undated', date: '' })
      expect(dayPillLabel(key)).toBe('Undated')
    }
  })
})

describe('topDay', () => {
  const headings = [
    { key: '2026-09-27', top: -300 },
    { key: '2026-09-26', top: 40 },
    { key: '2026-09-20', top: 400 },
  ]

  it('is null when no heading has reached the threshold', () => {
    expect(topDay(headings, -301)).toBeNull()
    expect(topDay([], 100)).toBeNull()
  })

  it('names the last heading in document order that has reached the threshold', () => {
    expect(topDay(headings, 100)).toBe('2026-09-26')
    expect(topDay(headings, 1000)).toBe('2026-09-20')
    expect(topDay(headings, -100)).toBe('2026-09-27')
  })

  it('counts a heading exactly at the threshold as reached', () => {
    expect(topDay(headings, 40)).toBe('2026-09-26')
    expect(topDay(headings, 39.5)).toBe('2026-09-27')
  })
})
