import { describe, expect, it } from 'vitest'

import { buildHeatmap } from '@/lib/heatmap'

const today = new Date('2026-06-20T12:00:00Z')

describe('buildHeatmap', () => {
  it('produces weeks*7 cells ending today, all level 0 when empty', () => {
    const cells = buildHeatmap({}, today, 12)
    expect(cells).toHaveLength(84)
    expect(cells[cells.length - 1].date).toBe('2026-06-20')
    expect(cells.every((c) => c.level === 0 && c.count === 0)).toBe(true)
  })

  it('maps per-day counts to intensity levels', () => {
    const cells = buildHeatmap(
      { '2026-06-20': 1, '2026-06-19': 5, '2026-06-18': 10 },
      today,
      12,
    )
    const byDate = Object.fromEntries(cells.map((c) => [c.date, c]))
    expect(byDate['2026-06-20'].level).toBe(1)
    expect(byDate['2026-06-19'].level).toBe(3)
    expect(byDate['2026-06-18'].level).toBe(4)
  })
})
