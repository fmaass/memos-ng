import { describe, expect, it } from 'vitest'

import { blurhashToDataUrl, tileKind } from '@/lib/media'

describe('tileKind', () => {
  // one case per input category — a new status must not silently fall through
  it('maps failed → failed', () => {
    expect(tileKind('failed', 'image/jpeg')).toBe('failed')
  })
  it('maps uploading/pending → placeholder', () => {
    expect(tileKind('uploading', 'image/jpeg')).toBe('placeholder')
    expect(tileKind('pending', 'video/mp4')).toBe('placeholder')
  })
  it('maps processing → processing', () => {
    expect(tileKind('processing', 'video/mp4')).toBe('processing')
  })
  it('maps ready image vs ready video by mime', () => {
    expect(tileKind('ready', 'image/png')).toBe('image')
    expect(tileKind('ready', 'video/mp4')).toBe('video')
  })
})

describe('blurhashToDataUrl', () => {
  it('returns null for a missing hash', () => {
    expect(blurhashToDataUrl(null)).toBeNull()
    expect(blurhashToDataUrl('')).toBeNull()
  })
  it('returns null for an invalid hash rather than throwing', () => {
    expect(blurhashToDataUrl('not-a-real-blurhash')).toBeNull()
  })
})
