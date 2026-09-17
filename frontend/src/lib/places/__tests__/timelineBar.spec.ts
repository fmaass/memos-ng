import { describe, expect, it, vi } from 'vitest'

import { buildHistogram, type TimelineState } from '@/lib/places/timeline'
import {
  BAR_HEIGHT,
  BAR_WIDTH,
  barControls,
  barGeometry,
  createTicker,
  indexAtX,
  isTypingTarget,
  MODE_OPTIONS,
  speedLabel,
  TICK_MS,
  timelineKeyAction,
  yearTickGeometry,
} from '@/lib/places/timelineBar'

import { marker } from './fixtures'

// Dec 2024 → Feb 2025: a year boundary, so the year ticks have something to separate.
const markers = [
  marker({ uid: 'a', taken_at: '2024-12-10T12:00:00Z' }),
  marker({ uid: 'b', taken_at: '2025-01-10T12:00:00Z' }),
  marker({ uid: 'c', taken_at: '2025-01-20T12:00:00Z' }),
  marker({ uid: 'd', taken_at: '2025-02-10T12:00:00Z' }),
]
const histogram = buildHistogram(markers)
const state = (over: Partial<TimelineState> = {}): TimelineState => ({
  index: 0,
  playing: false,
  speed: 1,
  mode: 'accumulate',
  bucketCount: histogram.buckets.length,
  ...over,
})

describe('barGeometry', () => {
  it('gives every month an equal slice of the width, in bucket order', () => {
    const geometry = barGeometry(histogram, { index: 0 })
    expect(geometry.bars.map((bar) => bar.key)).toEqual(['2024-12', '2025-01', '2025-02'])
    expect(geometry.bucketWidth).toBeCloseTo(BAR_WIDTH / 3)
    expect(geometry.bars.map((bar) => bar.x)).toEqual([0, BAR_WIDTH / 3, (BAR_WIDTH * 2) / 3])
    expect(geometry.bars.every((bar) => bar.width === geometry.bucketWidth)).toBe(true)
  })

  it('scales the tallest month to the full height and keeps a thin month visible', () => {
    const geometry = barGeometry(histogram, { index: 0 })
    const [december, january] = geometry.bars
    expect(january.height).toBe(BAR_HEIGHT) // 2 photos, the busiest month
    expect(december.height).toBeCloseTo(BAR_HEIGHT / 2)
    expect(december.y).toBeCloseTo(BAR_HEIGHT - december.height)
  })

  it('draws an empty month as nothing at all, and a counted month as at least a sliver', () => {
    const sparse = buildHistogram([
      marker({ uid: 'x', taken_at: '2024-01-10T12:00:00Z' }),
      ...Array.from({ length: 400 }, (_unused, n) =>
        marker({ uid: `y${n}`, taken_at: '2024-03-10T12:00:00Z' }),
      ),
    ])
    const [january, february, march] = barGeometry(sparse, { index: 0 }).bars
    expect(february.count).toBe(0)
    expect(february.height).toBe(0)
    expect(january.height).toBeGreaterThan(0) // 1 of 400 rounds to nothing without a floor
    expect(march.height).toBe(BAR_HEIGHT)
  })

  it('marks the months the playhead has passed, which is what the bar shades', () => {
    expect(barGeometry(histogram, { index: 1 }).bars.map((bar) => bar.past)).toEqual([
      true,
      true,
      false,
    ])
  })

  it('puts the playhead inside the month it is on, and on the year tick that owns it', () => {
    const ticks = yearTickGeometry(histogram.buckets)
    expect(ticks).toEqual([
      { year: 2024, x: 0, width: BAR_WIDTH / 3 },
      { year: 2025, x: BAR_WIDTH / 3, width: (BAR_WIDTH * 2) / 3 },
    ])
    const onJanuary = barGeometry(histogram, { index: 1 }).playheadX
    const tick2025 = ticks[1]
    expect(onJanuary).toBeGreaterThanOrEqual(tick2025.x)
    expect(onJanuary).toBeLessThan(tick2025.x + tick2025.width)
    expect(barGeometry(histogram, { index: 0 }).playheadX).toBeLessThan(tick2025.x)
  })

  it('has nothing to draw for an empty timeline', () => {
    const empty = barGeometry(buildHistogram([]), { index: 0 })
    expect(empty.bars).toEqual([])
    expect(empty.playheadX).toBe(0)
  })
})

describe('indexAtX', () => {
  it('turns a click on the bar into the month under it, clamped to the range', () => {
    expect(indexAtX(0, BAR_WIDTH, 3)).toBe(0)
    expect(indexAtX(BAR_WIDTH / 2, BAR_WIDTH, 3)).toBe(1)
    expect(indexAtX(BAR_WIDTH - 1, BAR_WIDTH, 3)).toBe(2)
    expect(indexAtX(-40, BAR_WIDTH, 3)).toBe(0)
    expect(indexAtX(BAR_WIDTH + 40, BAR_WIDTH, 3)).toBe(2)
    expect(indexAtX(10, BAR_WIDTH, 0)).toBe(0)
  })
})

describe('barControls', () => {
  it('gives the desktop all three speeds and both modes', () => {
    const controls = barControls(state(), false)
    expect(controls.variant).toBe('full')
    expect(controls.speeds).toEqual([1, 2, 4])
    expect(controls.chip).toBeNull()
    expect(controls.modes).toEqual(MODE_OPTIONS)
  })

  it('gives the phone one chip that names the speed and the next one it cycles to', () => {
    const controls = barControls(state({ speed: 2 }), true)
    expect(controls.variant).toBe('compact')
    expect(controls.speeds).toBeNull()
    expect(controls.chip).toEqual({ label: '2×', next: 4 })
    expect(controls.modes).toBeNull()
  })

  it('names the button for what pressing it does', () => {
    expect(barControls(state(), false).playLabel).toBe('Play')
    expect(barControls(state({ playing: true }), false).playLabel).toBe('Pause')
    expect(speedLabel(4)).toBe('4×')
  })
})

describe('timelineKeyAction', () => {
  it('claims space and the arrows when nobody is typing', () => {
    const paused = state()
    expect(timelineKeyAction(' ', paused, document.body)).toEqual({ type: 'play' })
    expect(timelineKeyAction(' ', state({ playing: true }), null)).toEqual({ type: 'pause' })
    expect(timelineKeyAction('ArrowRight', paused, document.body)).toEqual({
      type: 'seek',
      index: 1,
    })
    expect(timelineKeyAction('x', paused, document.body)).toBeNull()
  })

  it('leaves every key alone while a field has focus', () => {
    const paused = state()
    for (const tag of ['input', 'textarea', 'select']) {
      expect(timelineKeyAction(' ', paused, document.createElement(tag))).toBeNull()
    }
    const editable = document.createElement('div')
    editable.setAttribute('contenteditable', 'true')
    expect(isTypingTarget(editable)).toBe(true)
    expect(timelineKeyAction('ArrowLeft', paused, editable)).toBeNull()
    expect(isTypingTarget(document.createElement('div'))).toBe(false)
  })
})

describe('createTicker', () => {
  it('ticks once per interval while running and stops dead on stop', () => {
    vi.useFakeTimers()
    const onTick = vi.fn()
    const ticker = createTicker(onTick)
    ticker.start()
    vi.advanceTimersByTime(TICK_MS * 3)
    expect(onTick).toHaveBeenCalledTimes(3)
    ticker.stop()
    vi.advanceTimersByTime(TICK_MS * 5)
    expect(onTick).toHaveBeenCalledTimes(3)
    vi.useRealTimers()
  })

  it('starting twice does not run two clocks — that is a double-speed timeline', () => {
    vi.useFakeTimers()
    const onTick = vi.fn()
    const ticker = createTicker(onTick)
    ticker.start()
    ticker.start()
    vi.advanceTimersByTime(TICK_MS * 2)
    expect(onTick).toHaveBeenCalledTimes(2)
    ticker.stop()
    vi.useRealTimers()
  })

  it('keeps one interval for every speed: speed advances buckets, not the clock', () => {
    expect(TICK_MS).toBe(600)
  })
})
