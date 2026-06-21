import { describe, expect, it } from 'vitest'

import {
  DOUBLE_TAP_ZOOM,
  type ImageGeom,
  type Pt,
  clampOffset,
  distance,
  initState,
  isPannableX,
  maxOffset,
  midpoint,
  onDown,
  onMove,
  onUp,
} from '@/lib/lightboxGesture'

// A landscape image that exactly fits the viewport at scale 1 (no pan slack until zoomed).
const FIT: ImageGeom = { viewW: 1000, viewH: 800, imgW: 1000, imgH: 800 }
const p = (x: number, y: number): Pt => ({ x, y })

describe('geometry helpers', () => {
  it('maxOffset is 0 at fit and grows with scale', () => {
    expect(maxOffset('x', FIT, 1)).toBe(0)
    expect(maxOffset('x', FIT, 2)).toBe(500) // (1000*2 - 1000)/2
  })
  it('clampOffset clamps symmetrically', () => {
    expect(clampOffset(600, 500)).toBe(500)
    expect(clampOffset(-600, 500)).toBe(-500)
    expect(clampOffset(120, 500)).toBe(120)
  })
  it('isPannableX is false at fit, true when zoomed', () => {
    expect(isPannableX(FIT, 1)).toBe(false)
    expect(isPannableX(FIT, 2)).toBe(true)
  })
  it('distance and midpoint', () => {
    expect(distance(p(0, 0), p(3, 4))).toBe(5)
    expect(midpoint(p(0, 0), p(10, 20))).toEqual({ x: 5, y: 10 })
  })
})

describe('swipe at scale 1 navigates', () => {
  it('a drag past the commit ratio navigates (dragging right → previous)', () => {
    let r = onDown(initState(), [p(500, 400)], 0)
    r = onMove(r.state, [p(900, 400)], 16, FIT) // dx=400 (> hysteresis, not pannable → swipe)
    expect(r.state.mode).toBe('swipe')
    r = onUp(r.state, [], 32, FIT) // 400 > 0.3*1000
    expect(r.action).toEqual({ type: 'navigate', dir: -1 })
  })
  it('dragging left → next', () => {
    let r = onDown(initState(), [p(500, 400)], 0)
    r = onMove(r.state, [p(100, 400)], 16, FIT)
    r = onUp(r.state, [], 200, FIT)
    expect(r.action).toEqual({ type: 'navigate', dir: 1 })
  })
  it('OVERSTEER REGRESSION: a small slow drag does NOT navigate', () => {
    let r = onDown(initState(), [p(500, 400)], 0)
    r = onMove(r.state, [p(540, 400)], 100, FIT) // 40px on a 1000px viewport (old 50px bug zone)
    r = onUp(r.state, [], 300, FIT) // slow (velocity ~0), 40 < 300 → snapback, not navigate
    expect(r.action.type).toBe('snapback')
  })
})

describe('pan when zoomed never navigates (modal rule)', () => {
  it('horizontal drag pans the zoomed image, clamped to bounds', () => {
    const s = { ...initState(), scale: 2 }
    let r = onDown(s, [p(500, 400)], 0)
    r = onMove(r.state, [p(560, 400)], 16, FIT) // pannable at scale 2 → pan
    expect(r.state.mode).toBe('pan')
    expect(r.state.offsetX).toBe(60)
    expect(r.action.type).toBe('transform')
    // drag past the bound clamps; release snaps back (and never navigates)
    r = onMove(r.state, [p(2000, 400)], 32, FIT)
    expect(r.state.offsetX).toBe(500) // clamped to maxOffset
    r = onUp(r.state, [], 48, FIT)
    expect(r.action.type).toBe('snapback')
  })
})

describe('pinch scales (clamped)', () => {
  it('fingers apart zoom in; together clamp to fit; one lift continues as pan', () => {
    let r = onDown(initState(), [p(400, 400), p(600, 400)], 0) // startDist 200
    expect(r.state.mode).toBe('pinch')
    r = onMove(r.state, [p(300, 400), p(700, 400)], 16, FIT) // dist 400 → 2x
    expect(r.state.scale).toBe(2)
    r = onMove(r.state, [p(480, 400), p(520, 400)], 32, FIT) // dist 40 → 0.2 clamps to 1
    expect(r.state.scale).toBe(1)
    r = onUp(r.state, [p(480, 400)], 48, FIT) // one finger up → re-latch, no jump
    expect(r.state.mode).toBe('pending')
  })
})

describe('double-tap toggles zoom', () => {
  it('two quick taps at the same spot zoom in toward the tap', () => {
    let r = onDown(initState(), [p(500, 400)], 0)
    r = onUp(r.state, [], 50, FIT) // first tap recorded
    expect(r.action.type).toBe('none')
    r = onDown(r.state, [p(505, 405)], 100)
    r = onUp(r.state, [], 150, FIT) // within 300ms and 25px → double-tap
    expect(r.action.type).toBe('transform')
    expect(r.state.scale).toBe(DOUBLE_TAP_ZOOM)
  })
  it('double-tap while zoomed resets to fit', () => {
    let r = onDown({ ...initState(), scale: 2.5 }, [p(500, 400)], 0)
    r = onUp(r.state, [], 50, FIT)
    r = onDown(r.state, [p(500, 400)], 100)
    r = onUp(r.state, [], 150, FIT)
    expect(r.state.scale).toBe(1)
    expect(r.state.offsetX).toBe(0)
  })
  it('a slow second tap is NOT a double-tap (no zoom)', () => {
    let r = onDown(initState(), [p(500, 400)], 0)
    r = onUp(r.state, [], 50, FIT)
    r = onDown(r.state, [p(500, 400)], 500) // 450ms later → outside the window
    r = onUp(r.state, [], 520, FIT)
    expect(r.action.type).toBe('none')
    expect(r.state.scale).toBe(1)
  })
})

describe('axis lock + hysteresis', () => {
  it('a tiny drag stays pending (no pan, no navigate)', () => {
    let r = onDown(initState(), [p(500, 400)], 0)
    r = onMove(r.state, [p(505, 400)], 16, FIT) // 5px < hysteresis
    expect(r.state.mode).toBe('pending')
    expect(r.action.type).toBe('none')
  })
  it('a dominant-vertical drag at fit locks to y and does not navigate', () => {
    let r = onDown(initState(), [p(500, 400)], 0)
    r = onMove(r.state, [p(505, 470)], 16, FIT) // dy 70 > dx 5 → axis y
    expect(r.state.axis).toBe('y')
    r = onUp(r.state, [], 32, FIT)
    expect(r.action.type).not.toBe('navigate')
  })
})
