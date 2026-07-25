// Geometry of the derived image tiers the API serves. The worker fits each tier INSIDE a square box
// of its max edge and never upscales (`Image.thumbnail((MAX, MAX))` in app/services/media.py), so a
// tier's real pixel width depends on the image's own shape: a portrait is constrained by its height
// and its width lands far below the cap. Both consumers — a tile's srcset descriptors and a lightbox
// slide's zoom ceiling — need that real number, so it is computed here once, pure and testable.

/** app/services/media.py THUMB_MAX / MOBILE_MAX: the longest edge each tier is fitted into. */
export const THUMB_MAX_EDGE = 800
export const MOBILE_MAX_EDGE = 2048

export interface TierSize {
  width: number
  height: number
}

/** The pixels a max-edge-constrained tier actually holds for an image of these intrinsic dims. */
export function tierSize(width: number, height: number, maxEdge: number): TierSize {
  const longest = Math.max(width, height)
  if (longest <= maxEdge) return { width, height } // smaller than the cap: stored unresized
  const scale = maxEdge / longest
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  }
}

/**
 * A tile's `srcset` over the two image tiers, each with the w-descriptor it truly has — the browser
 * can then pick the mobile tier for a wide tile or a dense screen instead of upscaling the thumb.
 *
 * Undefined when there is nothing trustworthy to declare: unknown dims (legacy rows awaiting the
 * metadata backfill), or an original so small that both tiers serve the same pixels as `src` already
 * does. A descriptor the file does not live up to is worse than none — the browser sizes its choice
 * off that number.
 */
export function imageSrcset(
  width: number | null | undefined,
  height: number | null | undefined,
  urls: { thumbnailUrl: string; previewUrl: string },
): string | undefined {
  if (width == null || height == null || width <= 0 || height <= 0) return undefined
  const thumb = tierSize(width, height, THUMB_MAX_EDGE).width
  const mobile = tierSize(width, height, MOBILE_MAX_EDGE).width
  if (mobile <= thumb) return undefined
  return `${urls.thumbnailUrl} ${thumb}w, ${urls.previewUrl} ${mobile}w`
}
