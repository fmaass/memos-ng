import { describe, expect, it } from 'vitest'

import { groupByMonth } from '@/lib/librarySections'

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
