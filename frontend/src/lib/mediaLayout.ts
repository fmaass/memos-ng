// How a memo's media area lays out, decided from the attachment count + intrinsic dims. Pure and
// DOM-free so every count/dims category is unit-tested directly (happy-dom can't measure boxes).
//
// The shapes follow what feeds converged on: a lone photo reads as a photo (its own ratio, content
// width, height-capped), two crop into a uniform two-up, three or more pack into justified rows —
// the same aspect-preserving packing Resources uses (`justifyRows`).

export interface LayoutAttachment {
  width: number | null
  height: number | null
}

/** A lone attachment may not grow taller than this; it narrows instead of cropping (~34–36rem band). */
export const SINGLE_MAX_HEIGHT_REM = 35
/** Box a lone attachment falls back to while its dims are unknown (legacy rows before the backfill). */
export const SINGLE_FALLBACK_WIDTH = 4
export const SINGLE_FALLBACK_HEIGHT = 3
/** Uniform box for the two-up grid: both tiles share one ratio and crop into it. */
export const PAIR_TILE_WIDTH = 1
export const PAIR_TILE_HEIGHT = 1
/** Row height the justified packing aims for at feed column width, and the gap between tiles. */
export const JUSTIFIED_TARGET_HEIGHT = 200
export const JUSTIFIED_GAP = 4

export type MediaLayout =
  | { mode: 'none' }
  | { mode: 'single'; width: number; height: number; cropped: boolean; maxHeightRem: number }
  | { mode: 'pair'; width: number; height: number }
  | { mode: 'justified'; targetHeight: number; gap: number }

/**
 * The aspect ratio a tile box should take, or null when the dims are unknown/unusable — null is the
 * signal to keep the square default rather than guess a shape.
 */
export function tileAspectRatio(
  width?: number | null,
  height?: number | null,
): number | null {
  if (width == null || height == null || width <= 0 || height <= 0) return null
  return width / height
}

function singleBox(item: LayoutAttachment) {
  const { width, height } = item
  // Unknown dims: a 4:3 box the photo crops into keeps the feed's rhythm predictable until the
  // metadata backfill fills width/height, at which point the real ratio takes over.
  if (width == null || height == null || width <= 0 || height <= 0) {
    return { width: SINGLE_FALLBACK_WIDTH, height: SINGLE_FALLBACK_HEIGHT, cropped: true }
  }
  return { width, height, cropped: false }
}

export function mediaLayout(items: readonly LayoutAttachment[]): MediaLayout {
  if (items.length === 0) return { mode: 'none' }
  if (items.length === 1) {
    return { mode: 'single', ...singleBox(items[0]), maxHeightRem: SINGLE_MAX_HEIGHT_REM }
  }
  if (items.length === 2) {
    return { mode: 'pair', width: PAIR_TILE_WIDTH, height: PAIR_TILE_HEIGHT }
  }
  return { mode: 'justified', targetHeight: JUSTIFIED_TARGET_HEIGHT, gap: JUSTIFIED_GAP }
}
