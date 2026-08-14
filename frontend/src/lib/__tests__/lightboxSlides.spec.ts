import { describe, expect, it } from 'vitest'

import { buildSlides, type LightboxItem, slideForFace } from '@/lib/lightboxSlides'

function item(over: Partial<LightboxItem> & Pick<LightboxItem, 'uid' | 'mime'>): LightboxItem {
  return {
    width: 4000,
    height: 3000,
    thumbnailUrl: `/a/${over.uid}/thumb`,
    previewUrl: `/a/${over.uid}/mobile`,
    webUrl: `/a/${over.uid}/web`,
    posterUrl: `/a/${over.uid}/poster`,
    downloadUrl: `/a/${over.uid}/original`,
    ...over,
  }
}

describe('buildSlides', () => {
  it("maps an image to the mobile tier with a thumb placeholder + THAT tier's dims", () => {
    // The 4000×3000 original is served at ≤2048 max edge, so the slide declares 2048×1536 — telling
    // PhotoSwipe 4000 wide would let the zoom run past the pixels the file has and just blur.
    const [s] = buildSlides([item({ uid: 'p1', mime: 'image/jpeg', mine: true })])
    expect(s).toMatchObject({
      uid: 'p1',
      type: 'image',
      src: '/a/p1/mobile',
      msrc: '/a/p1/thumb',
      width: 2048,
      height: 1536,
      mine: true,
    })
    expect(s.poster).toBeUndefined()
  })

  it('scales a portrait by its long edge too (the width lands below the cap)', () => {
    const [s] = buildSlides([item({ uid: 'p3', mime: 'image/jpeg', width: 3000, height: 4000 })])
    expect([s.width, s.height]).toEqual([1536, 2048])
  })

  it('leaves an image already smaller than the tier at its own size (no upscale)', () => {
    const [s] = buildSlides([item({ uid: 'p4', mime: 'image/png', width: 1600, height: 1200 })])
    expect([s.width, s.height]).toEqual([1600, 1200])
  })

  it('keeps a video at its probed dims — not zoomable, and the web tier keeps the ratio', () => {
    const [s] = buildSlides([item({ uid: 'v3', mime: 'video/mp4', width: 3840, height: 2160 })])
    expect([s.width, s.height]).toEqual([3840, 2160])
  })

  it('maps a video to the web mp4 tier + poster, no msrc', () => {
    const [s] = buildSlides([item({ uid: 'v1', mime: 'video/mp4' })])
    expect(s).toMatchObject({ uid: 'v1', type: 'video', src: '/a/v1/web', poster: '/a/v1/poster' })
    expect(s.msrc).toBeUndefined()
    expect(s.mine).toBe(false) // defaults when not provided
  })

  it('falls back to sane dims when width/height are null', () => {
    const img = buildSlides([item({ uid: 'p2', mime: 'image/png', width: null, height: null })])[0]
    expect([img.width, img.height]).toEqual([1600, 1200])
    const vid = buildSlides([item({ uid: 'v2', mime: 'video/quicktime', width: null, height: null })])[0]
    expect([vid.width, vid.height]).toEqual([1600, 900])
  })

  it('falls back rather than declaring a zero-pixel slide for non-positive dims', () => {
    const [s] = buildSlides([item({ uid: 'p5', mime: 'image/jpeg', width: 0, height: 0 })])
    expect([s.width, s.height]).toEqual([1600, 1200])
  })

  it('preserves order and carries uid for the delete/print-master callbacks', () => {
    const slides = buildSlides([
      item({ uid: 'a', mime: 'image/jpeg' }),
      item({ uid: 'b', mime: 'video/mp4' }),
      item({ uid: 'c', mime: 'image/png' }),
    ])
    expect(slides.map((s) => s.uid)).toEqual(['a', 'b', 'c'])
  })
})

describe('slideForFace', () => {
  const screen = () => buildSlides([item({ uid: 'p1', mime: 'image/jpeg' })])[0]

  it('shows the master itself — at the master’s own pixel size, so zoom reaches it', () => {
    const flipped = slideForFace(screen(), 'print', {
      downloadUrl: '/api/attachments/p1/rendition/print:glossy',
      width: 4032,
      height: 3024,
    })
    expect(flipped.src).toBe('/api/attachments/p1/rendition/print:glossy')
    expect([flipped.width, flipped.height]).toEqual([4032, 3024])
    expect(flipped.uid).toBe('p1') // still the same photo — only the face changed
  })

  it('keeps the screen slide’s box when the master could not be measured', () => {
    const base = screen()
    const flipped = slideForFace(base, 'print', {
      downloadUrl: '/x',
      width: null,
      height: null,
    })
    expect([flipped.width, flipped.height]).toEqual([base.width, base.height])
  })

  it('is the untouched screen slide for the screen face, or when there is no master', () => {
    const base = screen()
    expect(slideForFace(base, 'screen', { downloadUrl: '/x', width: 10, height: 10 })).toBe(base)
    expect(slideForFace(base, 'print', null)).toBe(base)
  })
})
