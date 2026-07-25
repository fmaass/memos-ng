import { describe, expect, it } from 'vitest'

import { imageSrcset, MOBILE_MAX_EDGE, THUMB_MAX_EDGE, tierSize } from '@/lib/mediaTiers'

const urls = { thumbnailUrl: '/a/x/thumb', previewUrl: '/a/x/mobile' }

describe('tierSize', () => {
  it('mirrors the caps the worker fits each tier into', () => {
    // app/services/media.py THUMB_MAX / MOBILE_MAX — descriptors are a lie if these drift.
    expect([THUMB_MAX_EDGE, MOBILE_MAX_EDGE]).toEqual([800, 2048])
  })

  it('fits a landscape original by its width (the long edge)', () => {
    // 4000×3000 at 800: ×0.2 → 800×600. At 2048: ×0.512 → 2048×1536.
    expect(tierSize(4000, 3000, 800)).toEqual({ width: 800, height: 600 })
    expect(tierSize(4000, 3000, 2048)).toEqual({ width: 2048, height: 1536 })
  })

  it('fits a portrait by its HEIGHT, leaving the served width far below the cap', () => {
    // 3000×4000 at 800: ×0.2 → 600×800, so the tier is 600 wide, not 800.
    expect(tierSize(3000, 4000, 800)).toEqual({ width: 600, height: 800 })
    expect(tierSize(3000, 4000, 2048)).toEqual({ width: 1536, height: 2048 })
  })

  it('takes a square to the cap on both edges', () => {
    expect(tierSize(2400, 2400, 800)).toEqual({ width: 800, height: 800 })
    expect(tierSize(2400, 2400, 2048)).toEqual({ width: 2048, height: 2048 })
  })

  it('never upscales an original that already fits the tier', () => {
    expect(tierSize(640, 480, 800)).toEqual({ width: 640, height: 480 })
    expect(tierSize(640, 480, 2048)).toEqual({ width: 640, height: 480 })
    expect(tierSize(800, 600, 800)).toEqual({ width: 800, height: 600 }) // exactly at the cap
  })

  it('keeps an extreme panorama at least a pixel tall', () => {
    expect(tierSize(8000, 200, 800)).toEqual({ width: 800, height: 20 })
    expect(tierSize(20000, 5, 800)).toEqual({ width: 800, height: 1 }) // 5 × 0.04 rounds to 0
  })
})

describe('imageSrcset', () => {
  it('describes a landscape by its capped long edge', () => {
    expect(imageSrcset(4000, 3000, urls)).toBe('/a/x/thumb 800w, /a/x/mobile 2048w')
  })

  it('describes a portrait by the width each tier really serves — neither is 800/2048', () => {
    expect(imageSrcset(3000, 4000, urls)).toBe('/a/x/thumb 600w, /a/x/mobile 1536w')
  })

  it('describes a square at the caps', () => {
    expect(imageSrcset(2400, 2400, urls)).toBe('/a/x/thumb 800w, /a/x/mobile 2048w')
  })

  it('caps the mobile candidate at the original when it sits between the two tiers', () => {
    // 1200×900: the thumb is downscaled to 800 wide, the mobile tier keeps the original 1200.
    expect(imageSrcset(1200, 900, urls)).toBe('/a/x/thumb 800w, /a/x/mobile 1200w')
  })

  it('omits the srcset while the intrinsic dims are unknown (legacy rows) — src alone stands', () => {
    expect(imageSrcset(null, null, urls)).toBeUndefined()
    expect(imageSrcset(3000, null, urls)).toBeUndefined()
    expect(imageSrcset(null, 4000, urls)).toBeUndefined()
    expect(imageSrcset(undefined, undefined, urls)).toBeUndefined()
  })

  it('omits the srcset for non-positive dims rather than emitting a 1w candidate', () => {
    expect(imageSrcset(0, 0, urls)).toBeUndefined()
    expect(imageSrcset(-3000, 2000, urls)).toBeUndefined()
  })

  it('omits the srcset when both tiers serve the same pixels as src (small original)', () => {
    expect(imageSrcset(640, 480, urls)).toBeUndefined()
    expect(imageSrcset(800, 800, urls)).toBeUndefined() // exactly the thumb cap
  })
})
