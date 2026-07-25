// Pure mapping from a memo's/Resources' attachments to PhotoSwipe slide data. Kept separate from the
// PhotoSwipe wiring (which needs a real DOM) so the logic — image vs video, which URL, dims, fallbacks
// — is unit-testable. The structural input accepts both AttachmentRef (memos) and AttachmentListItem
// (resources): they share these fields.

import { MOBILE_MAX_EDGE, tierSize } from '@/lib/mediaTiers'

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

// What the slide's `src` actually holds, which is what PhotoSwipe must be told: an image slide loads
// the MOBILE tier, and declaring the original's dims over it set the zoom ceiling at pixels the file
// does not have — zooming in past the tier just magnified blur. A video keeps its probed dims: it is
// not zoomable (isContentZoomable), so the numbers only give the slide its aspect, which the web
// transcode preserves. Null/unusable dims stay on the fallback box.
function servedSize(a: LightboxItem, isVideo: boolean): { width: number; height: number } | null {
  if (a.width == null || a.height == null || a.width <= 0 || a.height <= 0) return null
  if (isVideo) return { width: a.width, height: a.height }
  return tierSize(a.width, a.height, MOBILE_MAX_EDGE)
}

export function buildSlides(items: readonly LightboxItem[]): Slide[] {
  return items.map((a) => {
    const isVideo = a.mime.startsWith('video/')
    const served = servedSize(a, isVideo) ?? (isVideo ? FALLBACK_VIDEO : FALLBACK_IMAGE)
    return {
      uid: a.uid,
      mime: a.mime,
      mine: a.mine ?? false,
      type: isVideo ? 'video' : 'image',
      src: isVideo ? a.webUrl : a.previewUrl,
      msrc: isVideo ? undefined : a.thumbnailUrl,
      poster: isVideo ? a.posterUrl : undefined,
      width: served.width,
      height: served.height,
    }
  })
}
