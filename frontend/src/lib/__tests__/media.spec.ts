import { decode } from 'blurhash'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { blurhashToDataUrl, LruMap, tileKind } from '@/lib/media'

// The real decoder, counted: every call still decodes.
vi.mock('blurhash', async (importOriginal) => {
  const actual = await importOriginal<typeof import('blurhash')>()
  return { ...actual, decode: vi.fn(actual.decode) }
})

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

describe('blurhashToDataUrl per-uid cache', () => {
  const HASH = 'LEHV6nWB2yk8pyo0adR*.7kCMdnj'
  const OTHER = 'L6PZfSi_.AyE_3t7t7R**0o#DgR4'
  let n = 0
  beforeEach(() => {
    vi.mocked(decode).mockClear()
    // happy-dom has no canvas; each data URL is distinct so a stale hit would show.
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => ({
      createImageData: (w: number, h: number) => ({ data: new Uint8ClampedArray(w * h * 4) }),
      putImageData: () => {},
    })) as unknown as HTMLCanvasElement['getContext'])
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockImplementation(() => `data:,${++n}`)
  })
  afterEach(() => vi.restoreAllMocks())

  it('decodes a uid once and hands back the same data URL after', () => {
    const first = blurhashToDataUrl(HASH, 'cache-u1')
    expect(first).toMatch(/^data:/)
    expect(blurhashToDataUrl(HASH, 'cache-u1')).toBe(first)
    expect(vi.mocked(decode)).toHaveBeenCalledTimes(1)
  })

  it('decodes again when the same uid arrives with a different hash', () => {
    // Processing can finish after the first render: the hash a uid carries is not fixed.
    const first = blurhashToDataUrl(HASH, 'cache-u2')
    const second = blurhashToDataUrl(OTHER, 'cache-u2')
    expect(second).not.toBe(first)
    expect(vi.mocked(decode)).toHaveBeenCalledTimes(2)
  })

  it('does not cache without a uid (callers that name none decode as before)', () => {
    blurhashToDataUrl(HASH)
    blurhashToDataUrl(HASH)
    expect(vi.mocked(decode)).toHaveBeenCalledTimes(2)
  })

  it('does not cache a failure, so a later render can still produce the placeholder', () => {
    vi.mocked(HTMLCanvasElement.prototype.getContext).mockReturnValueOnce(null)
    expect(blurhashToDataUrl(HASH, 'cache-u3')).toBeNull()
    expect(blurhashToDataUrl(HASH, 'cache-u3')).toMatch(/^data:/)
  })
})

describe('LruMap', () => {
  it('holds at most its capacity, evicting the least recently used entry', () => {
    const m = new LruMap<string, number>(2)
    m.set('a', 1)
    m.set('b', 2)
    expect(m.get('a')).toBe(1) // a is now the most recent
    m.set('c', 3)
    expect(m.size).toBe(2)
    expect(m.get('b')).toBeUndefined()
    expect(m.get('a')).toBe(1)
    expect(m.get('c')).toBe(3)
  })

  it('overwrites a key in place without growing', () => {
    const m = new LruMap<string, number>(2)
    m.set('a', 1)
    m.set('a', 2)
    expect(m.size).toBe(1)
    expect(m.get('a')).toBe(2)
  })

  it('refuses a capacity below one', () => {
    expect(() => new LruMap(0)).toThrow(RangeError)
  })
})
