import { describe, expect, it } from 'vitest'

import { groupByDay } from '@/lib/timeline'

const now = new Date('2026-06-20T12:00:00Z')
const memo = (uid: string, iso: string) => ({ uid, created_at: iso })

describe('groupByDay', () => {
  it('buckets memos by day in feed order with Today/Yesterday labels', () => {
    const groups = groupByDay(
      [
        memo('a', '2026-06-20T10:00:00Z'),
        memo('b', '2026-06-20T08:00:00Z'),
        memo('c', '2026-06-19T22:00:00Z'),
        memo('d', '2026-06-01T09:00:00Z'),
      ],
      now,
    )
    expect(groups.map((g) => g.label)).toEqual(['Today', 'Yesterday', expect.stringContaining('Jun')])
    expect(groups[0].items.map((m) => m.uid)).toEqual(['a', 'b'])
    expect(groups[1].items.map((m) => m.uid)).toEqual(['c'])
    expect(groups[2].items.map((m) => m.uid)).toEqual(['d'])
  })

  it('returns an empty list for no memos', () => {
    expect(groupByDay([], now)).toEqual([])
  })
})
