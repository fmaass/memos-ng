// The Places timeline — pure playback logic, no Vue, no MapLibre, no DOM (MEMNG Places Phase 4).
//
// Three ideas carry the module:
//
//  * a bucket is a CALENDAR MONTH in Europe/Zurich, not a UTC month and not a fixed 30 days. The
//    household reads its own clock, so a photo taken at half past midnight on 1 January belongs to
//    January even though UTC still says December, and the month around a DST change is 743 or 745
//    hours long. Zone maths reuses the SPA's existing helpers (`localDateIn`, `resolveCivil`) —
//    ADR-0007, and the SPA carries no date library to add one to;
//  * the reducer is the whole playback contract: nothing here reads a clock or sets a timer. The
//    component drives `tick`, which is why "reaching the end pauses" is a REDUCER rule and cannot
//    be forgotten by a caller;
//  * `applyPlayhead` answers with the visible set AND the ids that were not visible one state ago
//    (the `landing` set), so the map layer can be fed by `source.setData` — a MapLibre layer
//    `filter` does not re-cluster (mapbox-gl-js #2476) — and the new pins can be animated without
//    the caller diffing anything itself.

import type { components } from '@/api/schema'
import { resolveCivil } from '@/lib/autoMemoDate'
import { localDateIn } from '@/lib/digest'

export type MapMarker = components['schemas']['MapMarker']

/** Buckets and labels are the household's calendar, fixed — not the browser's zone (ADR-0007). */
export const TIMELINE_ZONE = 'Europe/Zurich'

/** The second mode shows a trailing year rather than everything seen so far. */
export const WINDOW_MONTHS = 12

export const SPEEDS = [1, 2, 4] as const
export type PlaybackSpeed = (typeof SPEEDS)[number]

export type TimelineMode = 'accumulate' | 'window12'

export interface Bucket {
  /** `YYYY-MM` in `TIMELINE_ZONE` — sortable, and the histogram's identity for a month. */
  key: string
  year: number
  /** 1…12. */
  month: number
  /** Inclusive instant the month starts at in the zone. */
  startMs: number
  /** Exclusive instant the next month starts at — the two differ by 743 h / 745 h at a DST change. */
  endMs: number
  count: number
}

export interface Histogram {
  zone: string
  /** Contiguous months from the earliest to the latest dated marker; empty months included. */
  buckets: Bucket[]
  /** Dated markers only — an undated one is counted nowhere. */
  total: number
  maxCount: number
  /** Uids whose `taken_at` did not parse: placeable nowhere in time, so never hidden either. */
  undated: string[]
}

export interface TimelineState {
  /** Index into `Histogram.buckets`; 0 when there are none. */
  index: number
  playing: boolean
  speed: PlaybackSpeed
  mode: TimelineMode
  bucketCount: number
}

export type TimelineAction =
  | { type: 'play' }
  | { type: 'pause' }
  | { type: 'tick' }
  | { type: 'seek'; index: number }
  | { type: 'setSpeed'; speed: PlaybackSpeed }
  | { type: 'setMode'; mode: TimelineMode }

export interface PlayheadView {
  /** The visible markers, in the order they were given — the caller keeps its own sort. */
  markers: MapMarker[]
  /** Uids visible now that were not visible in `options.previous`; empty without one. */
  landing: string[]
}

export interface YearTick {
  year: number
  /** First bucket index of the year — a tick's position is proportional to it. */
  startIndex: number
  /** How many buckets the year covers here, so the tick can be drawn to width. */
  span: number
}

const pad2 = (n: number): string => String(n).padStart(2, '0')

/** The `YYYY-MM` the zone's calendar puts an instant in, or null when the wire value is no date. */
function monthKeyOf(takenAt: string, zone: string): string | null {
  const ms = Date.parse(takenAt)
  if (!Number.isFinite(ms)) return null
  return localDateIn(zone, new Date(ms))?.slice(0, 7) ?? null
}

/** The instant a zone month begins at. `resolveCivil` verifies its own answer against the zone. */
function monthStartMs(year: number, month: number, zone: string): number {
  const resolved = resolveCivil(`${year}-${pad2(month)}-01T00:00:00`, zone)
  return resolved ? resolved.getTime() : Date.UTC(year, month - 1, 1)
}

const nextMonth = (year: number, month: number): [number, number] =>
  month === 12 ? [year + 1, 1] : [year, month + 1]

/**
 * Per-month counts over the markers' `taken_at`, as one contiguous run of calendar months.
 *
 * Empty months are kept: the bar draws time proportionally, and a gap that is silently closed up
 * would misplace every year tick after it.
 */
export function buildHistogram(
  markers: readonly MapMarker[],
  zone: string = TIMELINE_ZONE,
): Histogram {
  const counts = new Map<string, number>()
  const undated: string[] = []
  for (const marker of markers) {
    const key = monthKeyOf(marker.taken_at, zone)
    if (key === null) {
      undated.push(marker.uid)
      continue
    }
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  const keys = [...counts.keys()].sort()
  if (keys.length === 0) return { zone, buckets: [], total: 0, maxCount: 0, undated }

  const [firstYear, firstMonth] = keys[0].split('-').map(Number)
  const last = keys[keys.length - 1]
  const buckets: Bucket[] = []
  let year = firstYear
  let month = firstMonth
  let startMs = monthStartMs(year, month, zone)
  for (;;) {
    const key = `${year}-${pad2(month)}`
    const [ny, nm] = nextMonth(year, month)
    const endMs = monthStartMs(ny, nm, zone)
    buckets.push({ key, year, month, startMs, endMs, count: counts.get(key) ?? 0 })
    if (key === last) break
    year = ny
    month = nm
    startMs = endMs
  }
  return {
    zone,
    buckets,
    total: buckets.reduce((sum, b) => sum + b.count, 0),
    maxCount: buckets.reduce((max, b) => Math.max(max, b.count), 0),
    undated,
  }
}

/** The bar's label for a bucket, e.g. `Jan 2025`, read in the zone the bucket was cut in. */
export function bucketLabel(bucket: Bucket, zone: string = TIMELINE_ZONE): string {
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: zone,
    month: 'short',
    year: 'numeric',
  }).format(new Date(bucket.startMs))
}

/** The year ticks the desktop bar draws, each with the bucket span it covers. */
export function yearTicks(buckets: readonly Bucket[]): YearTick[] {
  const ticks: YearTick[] = []
  buckets.forEach((bucket, index) => {
    const open = ticks[ticks.length - 1]
    if (open && open.year === bucket.year) open.span += 1
    else ticks.push({ year: bucket.year, startIndex: index, span: 1 })
  })
  return ticks
}

/** The phone chip's single control: one tap moves 1× → 2× → 4× → 1×. */
export function nextSpeed(speed: PlaybackSpeed): PlaybackSpeed {
  return SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length]
}

/** Paused at the newest bucket — the Places view opens showing every pin, not an empty map. */
export function initialState(histogram: Histogram): TimelineState {
  return {
    index: Math.max(histogram.buckets.length - 1, 0),
    playing: false,
    speed: 1,
    mode: 'accumulate',
    bucketCount: histogram.buckets.length,
  }
}

function clampIndex(index: number, bucketCount: number, fallback: number): number {
  if (!Number.isFinite(index)) return fallback
  return Math.min(Math.max(Math.trunc(index), 0), Math.max(bucketCount - 1, 0))
}

/**
 * The playback contract. Nothing here schedules anything: `tick` is one step the caller drives, so
 * the speed is "how many buckets a step covers", and the end of the range pauses HERE rather than
 * in whichever component happens to own the timer.
 */
export function reduce(state: TimelineState, action: TimelineAction): TimelineState {
  const last = state.bucketCount - 1
  switch (action.type) {
    case 'play': {
      if (last < 0) return state
      // Pressing play with the playhead parked at the newest bucket (the default) means "replay",
      // not "advance nowhere".
      return { ...state, playing: true, index: state.index >= last ? 0 : state.index }
    }
    case 'pause':
      return state.playing ? { ...state, playing: false } : state
    case 'tick': {
      if (!state.playing) return state
      if (last < 0) return { ...state, playing: false }
      const next = state.index + state.speed
      if (next >= last) return { ...state, index: last, playing: false }
      return { ...state, index: next }
    }
    case 'seek':
      return { ...state, index: clampIndex(action.index, state.bucketCount, state.index) }
    case 'setSpeed':
      return { ...state, speed: action.speed }
    case 'setMode':
      // A mode switch re-reads the same instant in time: the playhead does not move.
      return { ...state, mode: action.mode }
  }
}

function visibleAt(bucketIndex: number | null, state: TimelineState): boolean {
  if (bucketIndex === null) return true // undated: placeable nowhere, so hidden nowhere
  if (state.mode === 'window12') {
    return bucketIndex <= state.index && bucketIndex > state.index - WINDOW_MONTHS
  }
  return bucketIndex <= state.index
}

/**
 * The markers the playhead exposes, plus the ones that arrived in this step.
 *
 * `options.histogram` lets a caller that already built the bar's histogram skip rebuilding it per
 * tick; omitted, the buckets are derived from the same markers, so the answer is the same either
 * way. `options.previous` is the state one step ago — without it nothing is "new", which is what a
 * first render wants (no landing animation on every pin at once).
 */
export function applyPlayhead(
  markers: readonly MapMarker[],
  state: TimelineState,
  options: { previous?: TimelineState | null; histogram?: Histogram } = {},
): PlayheadView {
  const histogram = options.histogram ?? buildHistogram(markers)
  const previous = options.previous ?? null
  const indexByKey = new Map(histogram.buckets.map((bucket, index) => [bucket.key, index]))
  const visible: MapMarker[] = []
  const landing: string[] = []
  for (const marker of markers) {
    const key = monthKeyOf(marker.taken_at, histogram.zone)
    const bucketIndex = key === null ? null : (indexByKey.get(key) ?? null)
    if (!visibleAt(bucketIndex, state)) continue
    visible.push(marker)
    if (previous && bucketIndex !== null && !visibleAt(bucketIndex, previous)) {
      landing.push(marker.uid)
    }
  }
  return { markers: visible, landing }
}

/** Space toggles playback, ←/→ step one bucket; any other key is not ours. */
export function actionForKey(key: string, state: TimelineState): TimelineAction | null {
  if (key === ' ' || key === 'Spacebar') return state.playing ? { type: 'pause' } : { type: 'play' }
  if (key === 'ArrowRight') return { type: 'seek', index: state.index + 1 }
  if (key === 'ArrowLeft') return { type: 'seek', index: state.index - 1 }
  return null
}
