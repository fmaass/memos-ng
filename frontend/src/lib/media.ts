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

// Decode a BlurHash into a small data: URL for the pending/processing placeholder.
// Returns null when the hash is absent or a canvas isn't available (SSR / minimal DOM),
// so callers fall back to a solid placeholder.
export function blurhashToDataUrl(hash: string | null | undefined, size = 32): string | null {
  if (!hash) return null
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
