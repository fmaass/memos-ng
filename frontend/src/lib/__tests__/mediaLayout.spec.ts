import { describe, expect, it } from 'vitest'

import {
  justifiedTileSizes,
  mediaLayout,
  singleTileMaxWidthPx,
  tileAspectRatio,
  tileSizes,
} from '@/lib/mediaLayout'

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

describe('singleTileMaxWidthPx', () => {
  it('narrows a portrait to what the 35rem height cap leaves it (3:4 → 26.25rem → 420px)', () => {
    expect(singleTileMaxWidthPx(3000, 4000)).toBe(420)
  })

  it('lets a landscape want more width than the column has (3:2 → 52.5rem → 840px)', () => {
    expect(singleTileMaxWidthPx(3000, 2000)).toBe(840)
  })

  it('holds a square at the height cap itself (35rem → 560px)', () => {
    expect(singleTileMaxWidthPx(2400, 2400)).toBe(560)
  })

  it('has no cap to give while the dims are unknown (that box is the cropped 4:3 fallback)', () => {
    expect(singleTileMaxWidthPx(null, null)).toBeNull()
    expect(singleTileMaxWidthPx(3000, null)).toBeNull()
    expect(singleTileMaxWidthPx(undefined, undefined)).toBeNull()
    expect(singleTileMaxWidthPx(0, 0)).toBeNull()
  })
})

describe('tileSizes', () => {
  // The column is 896px at xl and 672px below it (max-w-4xl / max-w-2xl), and the view's p-4 plus
  // the card's p-4 take 64px off both: a lone tile renders 832px / 608px wide.
  it('states the content-column width for a lone photo at each step of the column rule', () => {
    expect(tileSizes('single')).toBe(
      '(min-width: 1280px) 832px, (min-width: 672px) 608px, calc(100vw - 64px)',
    )
  })

  it('halves the column, less the gap, for a two-up tile', () => {
    // (832 - 8) / 2 = 412 at xl; (608 - 8) / 2 = 300 below it.
    expect(tileSizes('pair')).toBe(
      '(min-width: 1280px) 412px, (min-width: 672px) 300px, calc((100vw - 72px) / 2)',
    )
  })

  it('advertises the height-capped width for a portrait single, not the whole column', () => {
    // 3:4 against the 35rem cap renders 420px wide at every column step; below a 484px viewport
    // (420 + the 64px insets) the box is the viewport again.
    expect(tileSizes('single', 420)).toBe('(min-width: 484px) 420px, calc(100vw - 64px)')
  })

  it('leaves a wide landscape column-bound (its cap is wider than the column ever is)', () => {
    expect(tileSizes('single', 840)).toBe(
      '(min-width: 1280px) 832px, (min-width: 672px) 608px, calc(100vw - 64px)',
    )
  })

  it('applies a cap that binds only on the wide column', () => {
    // 5:4 → 700px: under the 832px xl media area, over the 608px one below it.
    expect(tileSizes('single', 700)).toBe(
      '(min-width: 1280px) 700px, (min-width: 672px) 608px, calc(100vw - 64px)',
    )
  })

  it('keeps a capless single (unknown dims) and every pair on the column width', () => {
    expect(tileSizes('single', null)).toBe(
      '(min-width: 1280px) 832px, (min-width: 672px) 608px, calc(100vw - 64px)',
    )
    expect(tileSizes('pair', null)).toBe(
      '(min-width: 1280px) 412px, (min-width: 672px) 300px, calc((100vw - 72px) / 2)',
    )
  })

  it('claims a wider tile at xl than below it, in both layouts (the column widens there)', () => {
    for (const mode of ['single', 'pair'] as const) {
      const [xl, mid] = tileSizes(mode).split(', ').map((s) => parseInt(s.split(') ')[1], 10))
      expect(xl).toBeGreaterThan(mid)
    }
  })
})

describe('justifiedTileSizes', () => {
  it("states the tile's own rendered width in whole px", () => {
    expect(justifiedTileSizes(266.6667)).toBe('267px')
    expect(justifiedTileSizes(300)).toBe('300px')
  })

  it('never emits a zero-width hint for a sliver of a tile', () => {
    expect(justifiedTileSizes(0.2)).toBe('1px')
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
