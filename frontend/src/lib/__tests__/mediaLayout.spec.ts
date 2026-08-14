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

/**
 * What a browser takes a `sizes` attribute to mean at a given viewport: the first clause whose
 * media condition matches wins, and its value is either a literal px or one of the two fluid
 * forms. The band assertions below are about the width the hint RESOLVES to, not its spelling.
 */
function resolveSizes(sizes: string, viewportPx: number): number {
  for (const entry of sizes.split(', ')) {
    const clause = /^(?:\(min-width: (\d+)px\) )?(.+)$/.exec(entry)
    if (!clause) throw new Error(`unparsable sizes entry: ${entry}`)
    const [, min, value] = clause
    if (min !== undefined && viewportPx < Number(min)) continue
    const literal = /^(\d+)px$/.exec(value)
    if (literal) return Number(literal[1])
    const single = /^calc\(100vw - (\d+)px\)$/.exec(value)
    if (single) return viewportPx - Number(single[1])
    const pair = /^calc\(\(100vw - (\d+)px\) \/ 2\)$/.exec(value)
    if (pair) return (viewportPx - Number(pair[1])) / 2
    throw new Error(`unsupported sizes value: ${value}`)
  }
  throw new Error(`no clause matched ${viewportPx}px in: ${sizes}`)
}

describe('tileSizes', () => {
  // The column is 896px at xl and 672px below it (max-w-4xl / max-w-2xl), and the view's p-4 plus
  // the card's p-4 take 64px off both: a lone tile renders 832px / 608px wide. From `md` (768px)
  // AppShell's 240px sidebar is in flow, so the column only reaches 672px at 912px — in between the
  // media area is the viewport less the sidebar and the insets (304px).
  it('states the content-column width for a lone photo at each step of the column rule', () => {
    expect(tileSizes('single')).toBe(
      '(min-width: 1280px) 832px, (min-width: 912px) 608px, ' +
        '(min-width: 768px) calc(100vw - 304px), (min-width: 672px) 608px, calc(100vw - 64px)',
    )
  })

  it('halves the column, less the gap, for a two-up tile', () => {
    // (832 - 8) / 2 = 412 at xl; (608 - 8) / 2 = 300 below it; in the sidebar band the pair splits
    // the viewport less 240 + 64 + the 8px gap.
    expect(tileSizes('pair')).toBe(
      '(min-width: 1280px) 412px, (min-width: 912px) 300px, ' +
        '(min-width: 768px) calc((100vw - 312px) / 2), (min-width: 672px) 300px, ' +
        'calc((100vw - 72px) / 2)',
    )
  })

  it('advertises the height-capped width for a portrait single, not the whole column', () => {
    // 3:4 against the 35rem cap renders 420px wide at every column step (the sidebar band included:
    // 420 is under even the band's narrowest 464px media area); below a 484px viewport (420 + the
    // 64px insets) the box is the viewport again.
    expect(tileSizes('single', 420)).toBe('(min-width: 484px) 420px, calc(100vw - 64px)')
  })

  it('leaves a wide landscape column-bound (its cap is wider than the column ever is)', () => {
    expect(tileSizes('single', 840)).toBe(
      '(min-width: 1280px) 832px, (min-width: 912px) 608px, ' +
        '(min-width: 768px) calc(100vw - 304px), (min-width: 672px) 608px, calc(100vw - 64px)',
    )
  })

  it('applies a cap that binds only on the wide column', () => {
    // 5:4 → 700px: under the 832px xl media area, over the 608px one below it — and over the
    // sidebar band's widest (607px), so the band stays fluid.
    expect(tileSizes('single', 700)).toBe(
      '(min-width: 1280px) 700px, (min-width: 912px) 608px, ' +
        '(min-width: 768px) calc(100vw - 304px), (min-width: 672px) 608px, calc(100vw - 64px)',
    )
  })

  it('takes over from the fluid band width where a mid-band cap starts binding', () => {
    // A square photo caps at 35rem × 1 = 560px. The band's media area passes 560 at 560 + 304 =
    // 864px, so the cap holds from there up and the fluid clause covers 768–863px. Below `md` the
    // same cap binds at 560 + 64 = 624px.
    expect(tileSizes('single', 560)).toBe(
      '(min-width: 864px) 560px, (min-width: 768px) calc(100vw - 304px), ' +
        '(min-width: 624px) 560px, calc(100vw - 64px)',
    )
  })

  it('keeps a capless single (unknown dims) and every pair on the column width', () => {
    expect(tileSizes('single', null)).toBe(
      '(min-width: 1280px) 832px, (min-width: 912px) 608px, ' +
        '(min-width: 768px) calc(100vw - 304px), (min-width: 672px) 608px, calc(100vw - 64px)',
    )
    expect(tileSizes('pair', null)).toBe(
      '(min-width: 1280px) 412px, (min-width: 912px) 300px, ' +
        '(min-width: 768px) calc((100vw - 312px) / 2), (min-width: 672px) 300px, ' +
        'calc((100vw - 72px) / 2)',
    )
  })

  it('claims a wider tile at xl than below it, in both layouts (the column widens there)', () => {
    for (const mode of ['single', 'pair'] as const) {
      const [xl, mid] = tileSizes(mode).split(', ').map((s) => parseInt(s.split(') ')[1], 10))
      expect(xl).toBeGreaterThan(mid)
    }
  })
})

describe('tileSizes across the sidebar band (768–912px)', () => {
  // AppShell (`hidden md:flex … w-60`) puts a 240px sidebar in flow from 768px up, so the reading
  // column loses 240px of the viewport until 768 + 672 = 912px, where its own cap takes over.
  it('drops a lone tile by the sidebar at the lower boundary (767 vs 768)', () => {
    const single = tileSizes('single')
    // 767: no sidebar, column already capped at 672 → 672 - 64 insets = 608.
    expect(resolveSizes(single, 767)).toBe(608)
    // 768: sidebar in flow → (768 - 240) - 64 = 464, not the 608 the column-only maths claimed.
    expect(resolveSizes(single, 768)).toBe(464)
  })

  it('reaches the full column width only at the upper boundary (911 vs 912)', () => {
    const single = tileSizes('single')
    // 911: (911 - 240) - 64 = 607 — one px short of the capped column.
    expect(resolveSizes(single, 911)).toBe(607)
    // 912: the column finally hits its 672px cap → 608.
    expect(resolveSizes(single, 912)).toBe(608)
    // and it stays there up to xl.
    expect(resolveSizes(single, 1279)).toBe(608)
  })

  it('tracks the sidebar-reduced viewport mid-band', () => {
    // 840: (840 - 240) - 64 = 536.
    expect(resolveSizes(tileSizes('single'), 840)).toBe(536)
  })

  it('splits the sidebar-reduced width between two-up tiles at both boundaries', () => {
    const pair = tileSizes('pair')
    expect(resolveSizes(pair, 767)).toBe(300) // (608 - 8) / 2
    expect(resolveSizes(pair, 768)).toBe(228) // (464 - 8) / 2
    expect(resolveSizes(pair, 840)).toBe(264) // (536 - 8) / 2
    expect(resolveSizes(pair, 911)).toBe(299.5) // (607 - 8) / 2
    expect(resolveSizes(pair, 912)).toBe(300)
  })

  it('holds a mid-band cap at both boundaries, and the fluid width under it', () => {
    const capped = tileSizes('single', 560) // a square photo under the 35rem height cap
    expect(resolveSizes(capped, 767)).toBe(560) // column 608 > cap
    expect(resolveSizes(capped, 768)).toBe(464) // band 464 < cap → the box is the band width
    expect(resolveSizes(capped, 863)).toBe(559)
    expect(resolveSizes(capped, 864)).toBe(560) // the cap binds from here up
    expect(resolveSizes(capped, 911)).toBe(560)
    expect(resolveSizes(capped, 912)).toBe(560)
  })

  it('holds a cap narrower than the whole band flat across it', () => {
    const portraitCap = tileSizes('single', 420) // 3:4 against the 35rem cap
    for (const w of [767, 768, 840, 911, 912]) expect(resolveSizes(portraitCap, w)).toBe(420)
  })

  it('never advertises more than the layout can render, anywhere in the band', () => {
    const single = tileSizes('single')
    const pair = tileSizes('pair')
    for (let w = 768; w < 912; w++) {
      // The layout truth, stated from AppShell rather than from the hint: sidebar, then the
      // column's cap, then the view's p-4 and the card's p-4 on both sides.
      const media = Math.min(w - 240, 672) - 64
      expect(resolveSizes(single, w)).toBe(media)
      expect(resolveSizes(pair, w)).toBe((media - 8) / 2)
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
