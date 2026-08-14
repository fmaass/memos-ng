// How a memo's media area lays out, decided from the attachment count + intrinsic dims. Pure and
// DOM-free so every count/dims category is unit-tested directly (happy-dom can't measure boxes).
//
// The shapes follow what feeds converged on: a lone photo reads as a photo (its own ratio, content
// width, height-capped), two crop into a uniform two-up, three or more pack into justified rows —
// the same aspect-preserving packing Resources uses (`justifyRows`).

import {
  COLUMN_MAX_PX,
  COLUMN_MAX_XL_PX,
  COLUMN_PAD_PX,
  COLUMN_XL_MIN_PX,
  REM_PX,
  SIDEBAR_MIN_PX,
  SIDEBAR_WIDTH_PX,
} from '@/lib/layout'

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
/** MemoCard's own `p-4` and the `gap-2` between the two-up tiles, mirrored for the `sizes` maths. */
export const CARD_PAD_PX = 16
export const PAIR_GAP_PX = 8

// A card's media area is the column minus the view's padding minus the card's own, on both sides.
const MEDIA_INSET_PX = 2 * COLUMN_PAD_PX + 2 * CARD_PAD_PX
const MEDIA_WIDTH_PX = COLUMN_MAX_PX - MEDIA_INSET_PX
const MEDIA_WIDTH_XL_PX = COLUMN_MAX_XL_PX - MEDIA_INSET_PX
// From `md` up the sidebar takes its width off the viewport before the column gets any, so the
// column only reaches its cap once the viewport can hold both (768 + 240 = 912px). Between `md` and
// here the media area is viewport-bound *and* a sidebar narrower than the fluid maths below `md`.
const COLUMN_FULL_MIN_PX = COLUMN_MAX_PX + SIDEBAR_WIDTH_PX
// What the viewport owes to something other than the media area, with and without the sidebar.
const OUTSIDE_PX = MEDIA_INSET_PX
const OUTSIDE_WITH_SIDEBAR_PX = MEDIA_INSET_PX + SIDEBAR_WIDTH_PX

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

/**
 * How wide a lone photo actually renders once the height cap binds: its ratio against
 * SINGLE_MAX_HEIGHT_REM — the same maths the card's max-width style applies to the box, so a
 * portrait that trades width away is not advertised at the full column. Null when the dims are
 * unknown: that box is the cropped 4:3 fallback, and it carries no srcset to size anyway.
 */
export function singleTileMaxWidthPx(
  width?: number | null,
  height?: number | null,
): number | null {
  const ratio = tileAspectRatio(width, height)
  return ratio === null ? null : ratio * SINGLE_MAX_HEIGHT_REM * REM_PX
}

/**
 * The `sizes` hint for a tile in the single/pair layouts: how wide it renders at each step of the
 * column rule, and below the column's cap the viewport minus whatever the media area does not get.
 * A `srcset` without it is guesswork — the browser assumes the tile is 100vw wide and over-fetches
 * on a wide screen.
 *
 * The viewport is not all the column's: from `md` up AppShell's sidebar is in flow, so between it
 * and COLUMN_FULL_MIN_PX the fluid width owes a sidebar as well as the insets. Skipping that term
 * asks for a candidate up to a whole sidebar too wide across the whole band.
 *
 * `maxWidthPx` (single only) is the height cap's width limit: past it the box stops growing with
 * the column, so the hint must too, or a portrait pulls the mobile tier for a 420px render.
 */
export function tileSizes(mode: 'single' | 'pair', maxWidthPx?: number | null): string {
  const share = (px: number) => (mode === 'pair' ? Math.floor((px - PAIR_GAP_PX) / 2) : px)
  const cap = maxWidthPx == null ? Infinity : Math.floor(maxWidthPx)
  const fixed = (mediaPx: number) => `${Math.min(share(mediaPx), cap)}px`
  // A media area of `100vw - outside`, and a pair tile is half of that once the gap is out.
  const fluid = (outside: number) =>
    mode === 'pair'
      ? `calc((100vw - ${outside + PAIR_GAP_PX}px) / 2)`
      : `calc(100vw - ${outside}px)`
  // Where a binding cap takes over from a fluid width: the viewport at which the media area first
  // grows past the cap. Only a single tile carries one (`maxWidthPx` is single-only).
  const capBinds = mode === 'single' && cap < share(MEDIA_WIDTH_PX)
  const capBindsAt = (outside: number) => cap + outside

  const steps: [number, string][] = [
    [COLUMN_XL_MIN_PX, fixed(MEDIA_WIDTH_XL_PX)],
    [COLUMN_FULL_MIN_PX, fixed(MEDIA_WIDTH_PX)],
  ]
  // The `md`→COLUMN_FULL_MIN_PX band: sidebar in flow, column still short of its cap.
  if (capBinds && capBindsAt(OUTSIDE_WITH_SIDEBAR_PX) <= SIDEBAR_MIN_PX) {
    // The cap already binds at the band's narrow end, so it holds across the whole band.
    steps.push([SIDEBAR_MIN_PX, `${cap}px`])
  } else {
    if (capBinds) steps.push([capBindsAt(OUTSIDE_WITH_SIDEBAR_PX), `${cap}px`])
    steps.push([SIDEBAR_MIN_PX, fluid(OUTSIDE_WITH_SIDEBAR_PX)])
  }
  // Below `md` the sidebar is a drawer and the column has the viewport to itself, up to its cap.
  steps.push([COLUMN_MAX_PX, fixed(MEDIA_WIDTH_PX)])
  if (capBinds) steps.push([capBindsAt(OUTSIDE_PX), `${cap}px`])

  // `sizes` is first-match-wins, so a run of equal widths collapses onto its LOWEST breakpoint —
  // keeping the widest one instead would drop the tile back to the fluid width in between.
  const clauses = steps
    .filter(([, width], i) => i === steps.length - 1 || width !== steps[i + 1][1])
    .map(([min, width]) => `(min-width: ${min}px) ${width}`)
  return [...clauses, fluid(OUTSIDE_PX)].join(', ')
}

/** A justified tile is already laid out at an exact px width, so it states that width verbatim. */
export function justifiedTileSizes(widthPx: number): string {
  return `${Math.max(1, Math.round(widthPx))}px`
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
