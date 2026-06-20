import { describe, expect, it } from 'vitest'

import { aspectRatio, justifyRows } from '@/lib/justified'

describe('aspectRatio', () => {
  it('returns w/h for valid dims, 1 for unknown/invalid', () => {
    expect(aspectRatio(800, 400)).toBe(2)
    expect(aspectRatio(null, 400)).toBe(1)
    expect(aspectRatio(0, 400)).toBe(1)
    expect(aspectRatio(800, null)).toBe(1)
  })
})

describe('justifyRows', () => {
  it('packs squares to fill the row width exactly at/below target height', () => {
    const items = [
      { width: 100, height: 100 },
      { width: 100, height: 100 },
      { width: 100, height: 100 },
    ]
    const rows = justifyRows(items, 300, 100, 0)
    expect(rows).toHaveLength(1)
    expect(rows[0].height).toBeCloseTo(100)
    // three squares at height 100 fill exactly 300 wide
    const total = rows[0].items.reduce((s, i) => s + i.width, 0)
    expect(total).toBeCloseTo(300)
  })

  it('preserves aspect ratio — wide images get more width than tall ones', () => {
    const rows = justifyRows([{ width: 1600, height: 900 }, { width: 600, height: 900 }], 1000, 300, 0)
    const wide = rows[0].items[0]
    const tall = rows[0].items[1]
    expect(wide.width).toBeGreaterThan(tall.width) // 16:9 wider than 2:3 at equal height
    expect(wide.height).toBeCloseTo(tall.height) // same row height
  })

  it('breaks into multiple rows when items exceed the width budget', () => {
    const square = { width: 100, height: 100 }
    const rows = justifyRows([square, square, square, square, square, square], 300, 100, 0)
    expect(rows.length).toBeGreaterThanOrEqual(2) // 6 squares / 3-per-row → 2 rows
    for (const r of rows) expect(r.height).toBeLessThanOrEqual(100 + 0.001)
  })

  it('keeps the trailing partial row at target height (not stretched full width)', () => {
    const rows = justifyRows([{ width: 100, height: 100 }], 900, 120, 0)
    expect(rows).toHaveLength(1)
    expect(rows[0].height).toBeCloseTo(120)
    expect(rows[0].items[0].width).toBeCloseTo(120) // square at h=120 → w=120, NOT 900
  })

  it('accounts for gaps between items', () => {
    const rows = justifyRows([{ width: 100, height: 100 }, { width: 100, height: 100 }], 210, 100, 10)
    // avail = 210 - 10 = 200; two squares → height 100, widths 100 each, + 10 gap = 210
    const total = rows[0].items.reduce((s, i) => s + i.width, 0) + 10
    expect(total).toBeCloseTo(210)
  })

  it('returns [] for empty input or zero width', () => {
    expect(justifyRows([], 300, 100, 0)).toEqual([])
    expect(justifyRows([{ width: 100, height: 100 }], 0, 100, 0)).toEqual([])
  })
})
