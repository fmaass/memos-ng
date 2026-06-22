// Pure mapping from a memo's/Resources' attachments to PhotoSwipe slide data. Kept separate from the
// PhotoSwipe wiring (which needs a real DOM) so the logic — image vs video, which URL, dims, fallbacks
// — is unit-testable. The structural input accepts both AttachmentRef (memos) and AttachmentListItem
// (resources): they share these fields.

export interface LightboxItem {
  uid: string
  mime: string
  mine?: boolean
  width?: number | null
  height?: number | null
  thumbnailUrl: string
  previewUrl: string
  webUrl: string
  posterUrl: string
  downloadUrl: string
}

export interface Slide {
  uid: string
  mime: string
  mine: boolean
  type: 'image' | 'video'
  src: string // image: mobile tier; video: mp4 web tier
  msrc?: string // low-res placeholder for images (thumb) — instant paint before the full src loads
  poster?: string // video poster frame
  width: number
  height: number
}

// PhotoSwipe needs per-slide width/height for its zoom canvas + aspect; attachments usually carry them
// (filled by the worker), but fall back to sane 4:3 / 16:9 so a slide without dims still opens.
const FALLBACK_IMAGE = { width: 1600, height: 1200 }
const FALLBACK_VIDEO = { width: 1600, height: 900 }

export function buildSlides(items: readonly LightboxItem[]): Slide[] {
  return items.map((a) => {
    const isVideo = a.mime.startsWith('video/')
    const fallback = isVideo ? FALLBACK_VIDEO : FALLBACK_IMAGE
    return {
      uid: a.uid,
      mime: a.mime,
      mine: a.mine ?? false,
      type: isVideo ? 'video' : 'image',
      src: isVideo ? a.webUrl : a.previewUrl,
      msrc: isVideo ? undefined : a.thumbnailUrl,
      poster: isVideo ? a.posterUrl : undefined,
      width: a.width ?? fallback.width,
      height: a.height ?? fallback.height,
    }
  })
}
