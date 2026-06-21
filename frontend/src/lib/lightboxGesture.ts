// Pure, DOM-free gesture reducer for the media lightbox — pinch-zoom + pan coexisting with
// swipe-to-navigate. Extracted from the component (per CLAUDE.md) so the load-bearing arbitration
// is unit-tested directly, not driven through happy-dom (which has no real PointerEvent pipeline).
//
// Industry-grade arbitration (YARL "modal" rule + PhotoSwipe constants): at scale 1 a horizontal
// drag NAVIGATES; once zoomed (scale > 1) a drag PANS the image and never navigates — you zoom back
// out to swipe. Two fingers always pinch. Double-tap toggles zoom. This removes the old "swipe
// oversteers so you can't pinch" behaviour, where any 50px drag flipped the image.
// (A PhotoSwipe-style edge-handoff — swipe-from-the-panned-edge to navigate while zoomed — is a
// possible future polish; modal mode is deliberately chosen first because it never misfires.)

export const AXIS_HYSTERESIS = 10 // px a drag must travel before its axis (pan vs swipe) commits
export const DOUBLE_TAP_DELAY = 300 // ms window for a double-tap
export const DOUBLE_TAP_DIST = 25 // px max separation between the two taps
export const SWIPE_COMMIT_RATIO = 0.3 // drag past 30% of the viewport width commits a slide change
export const FLICK_VELOCITY = 0.5 // px/ms — a fast flick commits a slide change regardless of distance
export const MAX_ZOOM = 4
export const DOUBLE_TAP_ZOOM = 2.5

export type Mode = 'idle' | 'pending' | 'pan' | 'swipe' | 'pinch'
export interface Pt {
  x: number
  y: number
}
export interface ImageGeom {
  viewW: number
  viewH: number
  imgW: number // displayed (object-contain) image size at scale 1
  imgH: number
}

export interface GestureState {
  mode: Mode
  scale: number // 1 = fit
  offsetX: number
  offsetY: number
  axis: 'x' | 'y' | null
  downX: number
  downY: number
  curX: number
  curY: number
  startOffsetX: number
  startOffsetY: number
  startScale: number
  startDist: number
  swipeDX: number
  prevX: number
  prevT: number
  lastTapTime: number
  lastTapX: number
  lastTapY: number
}

export type GestureAction =
  | { type: 'none' }
  | { type: 'transform' } // component reads scale/offset from the returned state
  | { type: 'navigate'; dir: -1 | 1 }
  | { type: 'snapback' }

export interface Reduced {
  state: GestureState
  action: GestureAction
}

export function initState(): GestureState {
  return {
    mode: 'idle',
    scale: 1,
    offsetX: 0,
    offsetY: 0,
    axis: null,
    downX: 0,
    downY: 0,
    curX: 0,
    curY: 0,
    startOffsetX: 0,
    startOffsetY: 0,
    startScale: 1,
    startDist: 0,
    swipeDX: 0,
    prevX: 0,
    prevT: 0,
    lastTapTime: 0,
    lastTapX: 0,
    lastTapY: 0,
  }
}

// --- pure geometry helpers (each independently testable) ---

/** Half the overflow of the scaled image past the viewport on an axis — the max |pan| offset.
 *  0 ⇒ the image fits that axis (no slack), so a drag there can't pan (and, on x, may navigate). */
export function maxOffset(axis: 'x' | 'y', g: ImageGeom, scale: number): number {
  const el = axis === 'x' ? g.imgW : g.imgH
  const view = axis === 'x' ? g.viewW : g.viewH
  return Math.max(0, (el * scale - view) / 2)
}

export function clampOffset(off: number, max: number): number {
  return Math.min(Math.max(off, -max), max)
}

/** Is the image wider than the viewport at this scale (so a horizontal drag should pan, not navigate)? */
export function isPannableX(g: ImageGeom, scale: number): boolean {
  return maxOffset('x', g, scale) > 0
}

export function distance(a: Pt, b: Pt): number {
  return Math.hypot(a.x - b.x, a.y - b.y)
}

export function midpoint(a: Pt, b: Pt): Pt {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
}

function clampScale(s: number): number {
  return Math.min(Math.max(s, 1), MAX_ZOOM)
}

// --- reducer entry points (the component drives these from Pointer Events) ---

export function onDown(s: GestureState, pts: Pt[], now: number): Reduced {
  if (pts.length >= 2) {
    return {
      state: {
        ...s,
        mode: 'pinch',
        axis: null,
        startDist: distance(pts[0], pts[1]),
        startScale: s.scale,
        startOffsetX: s.offsetX,
        startOffsetY: s.offsetY,
      },
      action: { type: 'none' },
    }
  }
  const p = pts[0]
  return {
    state: {
      ...s,
      mode: 'pending',
      axis: null,
      downX: p.x,
      downY: p.y,
      curX: p.x,
      curY: p.y,
      prevX: p.x,
      prevT: now,
      startOffsetX: s.offsetX,
      startOffsetY: s.offsetY,
      swipeDX: 0,
    },
    action: { type: 'none' },
  }
}

export function onMove(s: GestureState, pts: Pt[], now: number, g: ImageGeom): Reduced {
  // Two fingers → pinch (zoom about the image centre; offsets re-clamp as the scale grows).
  if (s.mode === 'pinch' && pts.length >= 2) {
    const scale = clampScale((distance(pts[0], pts[1]) / (s.startDist || 1)) * s.startScale)
    const offsetX = clampOffset(s.startOffsetX, maxOffset('x', g, scale))
    const offsetY = clampOffset(s.startOffsetY, maxOffset('y', g, scale))
    return { state: { ...s, scale, offsetX, offsetY }, action: { type: 'transform' } }
  }
  if (pts.length !== 1) return { state: s, action: { type: 'none' } }

  const p = pts[0]
  const dx = p.x - s.downX
  const dy = p.y - s.downY
  let st = { ...s, curX: p.x, curY: p.y, prevX: s.curX, prevT: s.prevT }

  // Commit an axis once the drag clears the hysteresis threshold.
  if (st.mode === 'pending') {
    if (Math.max(Math.abs(dx), Math.abs(dy)) < AXIS_HYSTERESIS) {
      return { state: st, action: { type: 'none' } }
    }
    const axis: 'x' | 'y' = Math.abs(dx) >= Math.abs(dy) ? 'x' : 'y'
    // Horizontal drag at fit (scale 1, not pannable) navigates; everything else pans (clamped).
    const mode: Mode = axis === 'x' && !isPannableX(g, st.scale) ? 'swipe' : 'pan'
    st = { ...st, axis, mode, prevX: p.x, prevT: now }
  }

  if (st.mode === 'pan') {
    return {
      state: {
        ...st,
        offsetX: clampOffset(st.startOffsetX + dx, maxOffset('x', g, st.scale)),
        offsetY: clampOffset(st.startOffsetY + dy, maxOffset('y', g, st.scale)),
      },
      action: { type: 'transform' },
    }
  }
  if (st.mode === 'swipe') {
    // No live track drag (decided on release) — just accumulate distance + sample velocity.
    return { state: { ...st, swipeDX: dx, prevX: p.x, prevT: now }, action: { type: 'none' } }
  }
  return { state: st, action: { type: 'none' } }
}

export function onUp(s: GestureState, pts: Pt[], now: number, g: ImageGeom): Reduced {
  const remaining = pts.length

  if (s.mode === 'pinch') {
    if (remaining === 1) {
      // One finger lifted mid-pinch: continue as a fresh single-pointer gesture (re-latch, no jump).
      const p = pts[0]
      return {
        state: {
          ...s,
          mode: 'pending',
          axis: null,
          downX: p.x,
          downY: p.y,
          curX: p.x,
          curY: p.y,
          prevX: p.x,
          prevT: now,
          startOffsetX: s.offsetX,
          startOffsetY: s.offsetY,
        },
        action: { type: 'none' },
      }
    }
    // Both lifted: settle — at/below fit, recentre.
    if (s.scale <= 1) {
      return { state: { ...s, mode: 'idle', scale: 1, offsetX: 0, offsetY: 0 }, action: { type: 'transform' } }
    }
    return { state: { ...s, mode: 'idle' }, action: { type: 'transform' } }
  }

  if (remaining > 0) return { state: s, action: { type: 'none' } }

  if (s.mode === 'pan') {
    const offsetX = clampOffset(s.offsetX, maxOffset('x', g, s.scale))
    const offsetY = clampOffset(s.offsetY, maxOffset('y', g, s.scale))
    return { state: { ...s, mode: 'idle', offsetX, offsetY }, action: { type: 'snapback' } }
  }

  if (s.mode === 'swipe') {
    const dt = now - s.prevT
    const velocity = dt > 0 ? (s.curX - s.prevX) / dt : 0
    const farEnough = Math.abs(s.swipeDX) > SWIPE_COMMIT_RATIO * g.viewW
    const flick = Math.abs(velocity) > FLICK_VELOCITY
    const idle = { ...s, mode: 'idle' as Mode, swipeDX: 0 }
    if ((farEnough || flick) && s.swipeDX !== 0) {
      return { state: idle, action: { type: 'navigate', dir: s.swipeDX < 0 ? 1 : -1 } }
    }
    return { state: idle, action: { type: 'snapback' } }
  }

  // mode === 'pending' → a tap (never crossed hysteresis). Detect double-tap to toggle zoom.
  const isDouble =
    now - s.lastTapTime < DOUBLE_TAP_DELAY &&
    distance({ x: s.curX, y: s.curY }, { x: s.lastTapX, y: s.lastTapY }) < DOUBLE_TAP_DIST
  if (isDouble) {
    if (s.scale > 1) {
      return {
        state: { ...s, mode: 'idle', scale: 1, offsetX: 0, offsetY: 0, lastTapTime: 0 },
        action: { type: 'transform' },
      }
    }
    // Zoom toward the tapped point: keep the content under the finger fixed (centre-origin transform).
    const scale = DOUBLE_TAP_ZOOM
    const offsetX = clampOffset((s.curX - g.viewW / 2) * (1 - scale), maxOffset('x', g, scale))
    const offsetY = clampOffset((s.curY - g.viewH / 2) * (1 - scale), maxOffset('y', g, scale))
    return {
      state: { ...s, mode: 'idle', scale, offsetX, offsetY, lastTapTime: 0 },
      action: { type: 'transform' },
    }
  }
  return {
    state: { ...s, mode: 'idle', lastTapTime: now, lastTapX: s.curX, lastTapY: s.curY },
    action: { type: 'none' },
  }
}
