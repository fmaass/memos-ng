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

  it('clamps the window start to the first activity (no leading empty weeks)', () => {
    const cells = buildHeatmap({ '2026-06-15': 2, '2026-06-18': 1 }, today, 12)
    expect(cells[0].date).toBe('2026-06-15') // starts at first activity, not 12 weeks back
    expect(cells[cells.length - 1].date).toBe('2026-06-20') // through today
    expect(cells).toHaveLength(6) // 06-15 .. 06-20, no leading gray block
  })

  it('still caps the window at `weeks` when activity is older', () => {
    const cells = buildHeatmap({ '2026-01-01': 1, '2026-06-20': 1 }, today, 12)
    expect(cells).toHaveLength(84) // capped at 12 weeks
    expect(cells.some((c) => c.date === '2026-01-01')).toBe(false) // the old memo is outside the cap
  })
})
