import { describe, expect, it } from 'vitest'

import { buildSlides, type LightboxItem } from '@/lib/lightboxSlides'

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
  it('maps an image to the mobile tier with a thumb placeholder + real dims', () => {
    const [s] = buildSlides([item({ uid: 'p1', mime: 'image/jpeg', mine: true })])
    expect(s).toMatchObject({
      uid: 'p1',
      type: 'image',
      src: '/a/p1/mobile',
      msrc: '/a/p1/thumb',
      width: 4000,
      height: 3000,
      mine: true,
    })
    expect(s.poster).toBeUndefined()
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

  it('preserves order and carries uid for delete/add-print callbacks', () => {
    const slides = buildSlides([
      item({ uid: 'a', mime: 'image/jpeg' }),
      item({ uid: 'b', mime: 'video/mp4' }),
      item({ uid: 'c', mime: 'image/png' }),
    ])
    expect(slides.map((s) => s.uid)).toEqual(['a', 'b', 'c'])
  })
})
