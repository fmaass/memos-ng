import { describe, expect, it } from 'vitest'

import { mediaLayout, tileAspectRatio } from '@/lib/mediaLayout'

const landscape = { width: 3000, height: 2000 } // 3:2
const portrait = { width: 2000, height: 3000 } // 2:3
const unknown = { width: null, height: null }

describe('tileAspectRatio', () => {
  it('returns w/h for usable dims', () => {
    expect(tileAspectRatio(3000, 2000)).toBeCloseTo(1.5)
    expect(tileAspectRatio(2000, 3000)).toBeCloseTo(2 / 3)
  })

  it('returns null when dims are missing or unusable (caller keeps the square box)', () => {
    expect(tileAspectRatio(null, null)).toBeNull()
    expect(tileAspectRatio(3000, null)).toBeNull()
    expect(tileAspectRatio(null, 2000)).toBeNull()
    expect(tileAspectRatio(undefined, undefined)).toBeNull()
    expect(tileAspectRatio(0, 2000)).toBeNull()
    expect(tileAspectRatio(3000, 0)).toBeNull()
    expect(tileAspectRatio(-3000, 2000)).toBeNull()
  })
})

describe('mediaLayout', () => {
  it('lays out nothing for a memo without attachments', () => {
    expect(mediaLayout([])).toEqual({ mode: 'none' })
  })

  it('gives a lone landscape photo its own ratio, uncropped, under the height cap', () => {
    const l = mediaLayout([landscape])
    expect(l.mode).toBe('single')
    if (l.mode !== 'single') return
    expect(l.width / l.height).toBeCloseTo(1.5) // the photo's ratio, not a square/4:3 box
    expect(l.cropped).toBe(false)
    expect(l.maxHeightRem).toBe(35) // the agreed cap, inside the 34–36rem band
    expect(l.maxHeightRem).toBeGreaterThanOrEqual(34)
    expect(l.maxHeightRem).toBeLessThanOrEqual(36)
  })

  it('gives a lone portrait photo its own (tall) ratio, uncropped', () => {
    const l = mediaLayout([portrait])
    expect(l.mode).toBe('single')
    if (l.mode !== 'single') return
    expect(l.width / l.height).toBeCloseTo(2 / 3)
    expect(l.width / l.height).toBeLessThan(1) // taller than wide — the cap narrows the box
    expect(l.cropped).toBe(false)
  })

  it('falls back to a cropped 4:3 box for a lone photo with unknown dims', () => {
    for (const item of [unknown, { width: 3000, height: null }, { width: null, height: 2000 }]) {
      const l = mediaLayout([item])
      expect(l.mode).toBe('single')
      if (l.mode !== 'single') continue
      expect(l.width / l.height).toBeCloseTo(4 / 3)
      expect(l.cropped).toBe(true)
    }
  })

  it('treats non-positive dims as unknown rather than dividing by zero', () => {
    const l = mediaLayout([{ width: 0, height: 0 }])
    expect(l.mode).toBe('single')
    if (l.mode !== 'single') return
    expect(l.width / l.height).toBeCloseTo(4 / 3)
    expect(l.cropped).toBe(true)
  })

  it('pairs two photos into one uniform box regardless of their own ratios', () => {
    const mixed = mediaLayout([landscape, portrait])
    const nullDims = mediaLayout([unknown, unknown])
    expect(mixed.mode).toBe('pair')
    expect(nullDims.mode).toBe('pair')
    // Both tiles share one ratio (they crop into it) — the layout must not depend on the dims.
    expect(mixed).toEqual(nullDims)
    if (mixed.mode !== 'pair') return
    expect(mixed.width / mixed.height).toBeGreaterThan(0)
  })

  it('packs three or more photos into justified rows', () => {
    for (const count of [3, 4, 9]) {
      const l = mediaLayout(Array.from({ length: count }, () => landscape))
      expect(l.mode).toBe('justified')
      if (l.mode !== 'justified') continue
      expect(l.targetHeight).toBe(200) // ~200px rows at feed column width
      expect(l.gap).toBeGreaterThanOrEqual(0)
    }
  })

  it('still justifies when the dims are unknown (justifyRows squares them itself)', () => {
    expect(mediaLayout([unknown, unknown, unknown]).mode).toBe('justified')
    expect(mediaLayout([landscape, unknown, portrait, unknown]).mode).toBe('justified')
  })
})
