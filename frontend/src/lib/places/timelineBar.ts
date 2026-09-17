// Everything the timeline BAR decides, kept out of the component: bar and tick geometry, which
// controls a breakpoint gets, whose keystrokes are ours, and the clock that drives playback.
//
// The component is a MapLibre-adjacent overlay that happy-dom cannot lay out — it has no
// measured widths and no canvas — so the bar is drawn in a fixed viewBox and every rule that
// decides a pixel, a label or a tick lives here, where a test can read it straight.

import {
  type Bucket,
  type Histogram,
  actionForKey,
  nextSpeed,
  type PlaybackSpeed,
  SPEEDS,
  type TimelineAction,
  type TimelineMode,
  type TimelineState,
  yearTicks,
} from '@/lib/places/timeline'

/**
 * One bucket per tick at 1×; 2× and 4× advance TWO and FOUR buckets per tick rather than running
 * the clock faster, so the map does one `setData` per tick at every speed. The plan names no
 * interval; 600 ms reads as playback rather than a flicker and leaves a frame budget of 36 at 60 Hz.
 */
export const TICK_MS = 600

/** The bar is drawn in viewBox units and stretched by CSS: geometry is deterministic, not measured. */
export const BAR_WIDTH = 1000
export const BAR_HEIGHT = 100
/** A month with photos is never invisible next to a month with hundreds. */
export const MIN_BAR_HEIGHT = 2

export const MODE_OPTIONS: { label: string; value: TimelineMode }[] = [
  { label: 'All so far', value: 'accumulate' },
  { label: 'Past 12 months', value: 'window12' },
]

export interface BarRect {
  key: string
  index: number
  count: number
  x: number
  y: number
  width: number
  height: number
  /** The playhead has reached this month — the bar shades it as seen. */
  past: boolean
}

export interface BarGeometry {
  bars: BarRect[]
  bucketWidth: number
  /** Centre of the month the playhead is on, in viewBox units. */
  playheadX: number
}

export interface YearTickRect {
  year: number
  x: number
  width: number
}

export function barGeometry(
  histogram: Histogram,
  options: { index: number; width?: number; height?: number },
): BarGeometry {
  const width = options.width ?? BAR_WIDTH
  const height = options.height ?? BAR_HEIGHT
  const buckets = histogram.buckets
  const bucketWidth = width / Math.max(buckets.length, 1)
  const bars = buckets.map((bucket, index) => {
    const scaled = histogram.maxCount > 0 ? (bucket.count / histogram.maxCount) * height : 0
    const barHeight = bucket.count > 0 ? Math.max(scaled, MIN_BAR_HEIGHT) : 0
    return {
      key: bucket.key,
      index,
      count: bucket.count,
      x: index * bucketWidth,
      y: height - barHeight,
      width: bucketWidth,
      height: barHeight,
      past: index <= options.index,
    }
  })
  const playheadX = buckets.length === 0 ? 0 : (options.index + 0.5) * bucketWidth
  return { bars, bucketWidth, playheadX }
}

/** The year labels under the bar, each as wide as the months it covers — time drawn to scale. */
export function yearTickGeometry(
  buckets: readonly Bucket[],
  width: number = BAR_WIDTH,
): YearTickRect[] {
  const bucketWidth = width / Math.max(buckets.length, 1)
  return yearTicks(buckets).map((tick) => ({
    year: tick.year,
    x: tick.startIndex * bucketWidth,
    width: tick.span * bucketWidth,
  }))
}

/** The month under a click on the bar — the scrubber's only geometry input. */
export function indexAtX(x: number, width: number, bucketCount: number): number {
  if (bucketCount <= 0) return 0
  const index = Math.floor((x / width) * bucketCount)
  return Math.min(Math.max(index, 0), bucketCount - 1)
}

export function speedLabel(speed: PlaybackSpeed): string {
  return `${speed}×`
}

export interface BarControls {
  variant: 'compact' | 'full'
  /** Desktop: every speed as a SelectButton. Phone: null — there is one chip instead. */
  speeds: PlaybackSpeed[] | null
  /** Phone: the chip showing the current speed and the one a tap moves to. */
  chip: { label: string; next: PlaybackSpeed } | null
  modes: { label: string; value: TimelineMode }[] | null
  playLabel: string
  playIcon: string
}

/**
 * A phone bar has room for a play button, one speed chip and a label — not for a SelectButton
 * per axis. The mode toggle is the one control the phone drops (plan decision: compact bar).
 */
export function barControls(state: TimelineState, phone: boolean): BarControls {
  const playLabel = state.playing ? 'Pause' : 'Play'
  const playIcon = state.playing ? 'pi pi-pause' : 'pi pi-play'
  if (phone) {
    return {
      variant: 'compact',
      speeds: null,
      chip: { label: speedLabel(state.speed), next: nextSpeed(state.speed) },
      modes: null,
      playLabel,
      playIcon,
    }
  }
  return { variant: 'full', speeds: [...SPEEDS], chip: null, modes: MODE_OPTIONS, playLabel, playIcon }
}

const TYPING_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT'])

/** Space is the timeline's key and the text field's key; whoever is typing wins. */
export function isTypingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null
  if (!element || typeof element.tagName !== 'string') return false
  if (TYPING_TAGS.has(element.tagName)) return true
  return element.isContentEditable === true || element.getAttribute?.('contenteditable') === 'true'
}

export function timelineKeyAction(
  key: string,
  state: TimelineState,
  target: EventTarget | null,
): TimelineAction | null {
  if (isTypingTarget(target)) return null
  return actionForKey(key, state)
}

export interface Ticker {
  start(): void
  stop(): void
  readonly running: boolean
}

/**
 * The playback clock, apart from the view so its scheduling can be tested with fake timers.
 * Starting a running ticker is a no-op: two intervals would silently double the speed.
 */
export function createTicker(onTick: () => void, intervalMs: number = TICK_MS): Ticker {
  let handle: ReturnType<typeof setInterval> | null = null
  return {
    start() {
      if (handle !== null) return
      handle = setInterval(onTick, intervalMs)
    },
    stop() {
      if (handle === null) return
      clearInterval(handle)
      handle = null
    },
    get running() {
      return handle !== null
    },
  }
}
