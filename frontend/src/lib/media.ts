import { decode } from 'blurhash'

// How a memo attachment should render, derived from its processing status (+ mime for the
// ready case). Pure so the media-state matrix is unit-tested per input category.
export type TileKind = 'placeholder' | 'processing' | 'image' | 'video' | 'failed'

export function tileKind(status: string, mime: string): TileKind {
  if (status === 'failed') return 'failed'
  if (status === 'uploading' || status === 'pending') return 'placeholder'
  if (status === 'processing') return 'processing'
  return mime.startsWith('video/') ? 'video' : 'image' // ready
}

// A Map capped at `max` entries that forgets the least recently used one first: a Map iterates in
// insertion order, so re-inserting on every hit keeps the oldest entry at the front.
export class LruMap<K, V> {
  private readonly entries = new Map<K, V>()
  constructor(private readonly max: number) {
    if (!(max >= 1)) throw new RangeError(`LruMap capacity must be at least 1, got ${max}`)
  }
  get size(): number {
    return this.entries.size
  }
  get(key: K): V | undefined {
    if (!this.entries.has(key)) return undefined
    const value = this.entries.get(key) as V
    this.entries.delete(key)
    this.entries.set(key, value)
    return value
  }
  set(key: K, value: V): void {
    this.entries.delete(key)
    this.entries.set(key, value)
    if (this.entries.size > this.max) this.entries.delete(this.entries.keys().next().value as K)
  }
}

// Decoding is ~1 ms of main thread per tile, and a paged library remounts tiles on every filter
// flip, page load and route change. The whole library is under 2 k rows (1795 on 2026-09-22), so
// 4 k entries hold all of it while still bounding a session that browses for hours.
const BLURHASH_CACHE_MAX = 4096
const blurhashCache = new LruMap<string, { hash: string; size: number; url: string }>(
  BLURHASH_CACHE_MAX,
)

// Decode a BlurHash into a small data: URL for the pending/processing placeholder.
// Returns null when the hash is absent or a canvas isn't available (SSR / minimal DOM),
// so callers fall back to a solid placeholder.
// With a `uid` the result is remembered per attachment; the hash is part of the entry because a
// uid's hash can change (it is written when processing finishes), and so is the size. A failure is never remembered.
export function blurhashToDataUrl(
  hash: string | null | undefined,
  uid?: string,
  size = 32,
): string | null {
  if (!hash) return null
  if (uid !== undefined) {
    const hit = blurhashCache.get(uid)
    if (hit && hit.hash === hash && hit.size === size) return hit.url
  }
  const url = decodeToDataUrl(hash, size)
  if (url !== null && uid !== undefined) blurhashCache.set(uid, { hash, size, url })
  return url
}

function decodeToDataUrl(hash: string, size: number): string | null {
  try {
    const pixels = decode(hash, size, size)
    const canvas = document.createElement('canvas')
    canvas.width = size
    canvas.height = size
    const ctx = canvas.getContext('2d')
    if (!ctx) return null
    const image = ctx.createImageData(size, size)
    image.data.set(pixels)
    ctx.putImageData(image, 0, 0)
    return canvas.toDataURL()
  } catch {
    return null
  }
}
