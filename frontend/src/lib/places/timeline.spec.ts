import { describe, expect, it } from 'vitest'

import {
  TIMELINE_ZONE,
  actionForKey,
  applyPlayhead,
  buildHistogram,
  bucketLabel,
  initialState,
  nextSpeed,
  reduce,
  yearTicks,
  type MapMarker,
  type TimelineState,
} from './timeline'

/** A marker is only ever read for `uid` and `taken_at` here; the rest is the wire shape. */
function marker(uid: string, takenAt: string): MapMarker {
  return {
    blurhash: null,
    capture_civil: null,
    capture_instant: takenAt,
    capture_source: 'exif',
    downloadUrl: `/api/attachments/${uid}/download`,
    duration: null,
    hasLegacyPrint: false,
    hasPrint: false,
    height: 3024,
    latitude: 46.9,
    longitude: 7.44,
    memo_uid: `memo-${uid}`,
    mime: 'image/jpeg',
    mine: true,
    position: 0,
    posterUrl: `/api/attachments/${uid}/poster`,
    previewUrl: `/api/attachments/${uid}/preview`,
    printUrl: `/api/attachments/${uid}/print`,
    renditions: [],
    sha256: null,
    status: 'ready',
    taken_at: takenAt,
    thumbnailUrl: `/api/attachments/${uid}/thumb`,
    uid,
    webUrl: `/api/attachments/${uid}/web`,
    width: 4032,
  }
}

const utc = (iso: string) => Date.parse(iso)

/** Four markers spanning 2025-01 … 2026-03 — fifteen contiguous month buckets, indices 0…14. */
const SPAN: MapMarker[] = [
  marker('a', '2025-01-15T12:00:00Z'), // bucket 0  (2025-01)
  marker('b', '2025-06-15T12:00:00Z'), // bucket 5  (2025-06)
  marker('c', '2026-01-15T12:00:00Z'), // bucket 12 (2026-01)
  marker('d', '2026-03-15T12:00:00Z'), // bucket 14 (2026-03)
]
const SPAN_HISTOGRAM = buildHistogram(SPAN)
const uids = (markers: readonly MapMarker[]) => markers.map((m) => m.uid)

function stateAt(index: number, patch: Partial<TimelineState> = {}): TimelineState {
  return { ...initialState(SPAN_HISTOGRAM), index, ...patch }
}

describe('buildHistogram', () => {
  it('buckets by the Europe/Zurich calendar month, not the UTC one, across a year boundary', () => {
    // 22:30Z on 31 December is still 23:30 in Zurich (CET, UTC+1); 23:30Z is already 00:30 on
    // 1 January there — the year boundary the household sees, not the one UTC sees.
    const histogram = buildHistogram([
      marker('old-year', '2025-12-31T22:30:00Z'),
      marker('new-year', '2025-12-31T23:30:00Z'),
    ])
    expect(histogram.buckets.map((b) => b.key)).toEqual(['2025-12', '2026-01'])
    expect(histogram.buckets.map((b) => b.count)).toEqual([1, 1])
    expect(histogram.buckets[1].startMs).toBe(utc('2025-12-31T23:00:00Z'))
  })

  it('places a marker by Zurich wall time across the spring DST change', () => {
    // 2026-03-29 is the spring-forward Sunday: from then on Zurich is UTC+2, so 22:30Z on
    // 31 March is already 00:30 on 1 April locally.
    const histogram = buildHistogram([
      marker('march', '2026-03-31T21:30:00Z'),
      marker('april', '2026-03-31T22:30:00Z'),
    ])
    expect(histogram.buckets.map((b) => b.key)).toEqual(['2026-03', '2026-04'])
    expect(histogram.buckets.map((b) => b.count)).toEqual([1, 1])
  })

  it('places a marker by Zurich wall time across the autumn DST change', () => {
    // 2026-10-25 is the fall-back Sunday: Zurich is UTC+1 again, so 23:30Z on 31 October is
    // 00:30 on 1 November locally.
    const histogram = buildHistogram([
      marker('october', '2026-10-31T22:30:00Z'),
      marker('november', '2026-10-31T23:30:00Z'),
    ])
    expect(histogram.buckets.map((b) => b.key)).toEqual(['2026-10', '2026-11'])
    expect(histogram.buckets.map((b) => b.count)).toEqual([1, 1])
  })

  it('gives each bucket the Zurich month edges, so the DST months are 743 h and 745 h long', () => {
    const histogram = buildHistogram([
      marker('spring', '2026-03-15T12:00:00Z'),
      marker('autumn', '2026-10-15T12:00:00Z'),
    ])
    const march = histogram.buckets.find((b) => b.key === '2026-03')!
    const october = histogram.buckets.find((b) => b.key === '2026-10')!
    // 2026-03-01T00:00 CET = 2026-02-28T23:00Z; 2026-04-01T00:00 CEST = 2026-03-31T22:00Z.
    expect(march.startMs).toBe(utc('2026-02-28T23:00:00Z'))
    expect(march.endMs).toBe(utc('2026-03-31T22:00:00Z'))
    expect((march.endMs - march.startMs) / 3_600_000).toBe(743)
    // 2026-10-01T00:00 CEST = 2026-09-30T22:00Z; 2026-11-01T00:00 CET = 2026-10-31T23:00Z.
    expect(october.startMs).toBe(utc('2026-09-30T22:00:00Z'))
    expect(october.endMs).toBe(utc('2026-10-31T23:00:00Z'))
    expect((october.endMs - october.startMs) / 3_600_000).toBe(745)
  })

  it('counts every marker into contiguous months, empty months included', () => {
    expect(SPAN_HISTOGRAM.buckets).toHaveLength(15)
    expect(SPAN_HISTOGRAM.buckets[0].key).toBe('2025-01')
    expect(SPAN_HISTOGRAM.buckets[14].key).toBe('2026-03')
    expect(SPAN_HISTOGRAM.buckets.map((b) => b.count)).toEqual([
      1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 1,
    ])
    expect(SPAN_HISTOGRAM.total).toBe(4)
    expect(SPAN_HISTOGRAM.maxCount).toBe(1)
    expect(SPAN_HISTOGRAM.zone).toBe(TIMELINE_ZONE)
  })

  it('sums repeated months and reports the tallest bar', () => {
    const histogram = buildHistogram([
      marker('x', '2025-07-01T09:00:00Z'),
      marker('y', '2025-07-20T09:00:00Z'),
      marker('z', '2025-08-02T09:00:00Z'),
    ])
    expect(histogram.buckets.map((b) => b.count)).toEqual([2, 1])
    expect(histogram.maxCount).toBe(2)
  })

  it('keeps an undated marker out of the buckets without losing it', () => {
    const histogram = buildHistogram([marker('ok', '2025-05-04T10:00:00Z'), marker('bad', 'not-a-date')])
    expect(histogram.buckets).toHaveLength(1)
    expect(histogram.total).toBe(1)
    expect(histogram.undated).toEqual(['bad'])
  })

  it('has no buckets for an empty marker set', () => {
    const histogram = buildHistogram([])
    expect(histogram.buckets).toEqual([])
    expect(histogram.total).toBe(0)
    expect(histogram.maxCount).toBe(0)
  })
})

describe('bucketLabel and yearTicks', () => {
  it('labels a bucket by its Zurich month', () => {
    expect(bucketLabel(SPAN_HISTOGRAM.buckets[0])).toBe('Jan 2025')
    expect(bucketLabel(SPAN_HISTOGRAM.buckets[14])).toBe('Mar 2026')
  })

  it('groups the buckets into proportional year ticks', () => {
    expect(yearTicks(SPAN_HISTOGRAM.buckets)).toEqual([
      { year: 2025, startIndex: 0, span: 12 },
      { year: 2026, startIndex: 12, span: 3 },
    ])
  })
})

describe('reduce', () => {
  it('starts paused at the last bucket, 1x, accumulating', () => {
    expect(initialState(SPAN_HISTOGRAM)).toEqual({
      index: 14,
      playing: false,
      speed: 1,
      mode: 'accumulate',
      bucketCount: 15,
    })
  })

  it('plays from where it stands, and restarts when the playhead is already at the end', () => {
    expect(reduce(stateAt(3), { type: 'play' })).toMatchObject({ index: 3, playing: true })
    expect(reduce(stateAt(14), { type: 'play' })).toMatchObject({ index: 0, playing: true })
  })

  it('pauses without moving the playhead', () => {
    expect(reduce(stateAt(7, { playing: true }), { type: 'pause' })).toMatchObject({
      index: 7,
      playing: false,
    })
  })

  it('advances one bucket per tick at 1x and speed buckets per tick at 2x and 4x', () => {
    expect(reduce(stateAt(0, { playing: true }), { type: 'tick' }).index).toBe(1)
    expect(reduce(stateAt(0, { playing: true, speed: 2 }), { type: 'tick' }).index).toBe(2)
    expect(reduce(stateAt(0, { playing: true, speed: 4 }), { type: 'tick' }).index).toBe(4)
  })

  it('ignores a tick while paused', () => {
    const paused = stateAt(3)
    expect(reduce(paused, { type: 'tick' })).toEqual(paused)
  })

  it('pauses at the last bucket instead of running past it', () => {
    const state = reduce(stateAt(13, { playing: true, speed: 4 }), { type: 'tick' })
    expect(state).toMatchObject({ index: 14, playing: false })
    expect(reduce(state, { type: 'tick' })).toEqual(state)
  })

  it('clamps a seek to the bucket range and keeps everything else', () => {
    expect(reduce(stateAt(5), { type: 'seek', index: -4 }).index).toBe(0)
    expect(reduce(stateAt(5), { type: 'seek', index: 99 }).index).toBe(14)
    expect(reduce(stateAt(5, { playing: true, speed: 2 }), { type: 'seek', index: 9 })).toEqual({
      index: 9,
      playing: true,
      speed: 2,
      mode: 'accumulate',
      bucketCount: 15,
    })
  })

  it('keeps the playhead when the speed or the mode changes', () => {
    const playing = stateAt(6, { playing: true })
    expect(reduce(playing, { type: 'setSpeed', speed: 4 })).toMatchObject({
      index: 6,
      playing: true,
      speed: 4,
    })
    expect(reduce(playing, { type: 'setMode', mode: 'window12' })).toMatchObject({
      index: 6,
      playing: true,
      mode: 'window12',
    })
  })

  it('does not play an empty timeline', () => {
    const empty = initialState(buildHistogram([]))
    expect(empty).toMatchObject({ index: 0, bucketCount: 0 })
    expect(reduce(empty, { type: 'play' }).playing).toBe(false)
  })
})

describe('applyPlayhead', () => {
  it('accumulates every bucket up to and including the playhead', () => {
    expect(uids(applyPlayhead(SPAN, stateAt(0)).markers)).toEqual(['a'])
    expect(uids(applyPlayhead(SPAN, stateAt(5)).markers)).toEqual(['a', 'b'])
    expect(uids(applyPlayhead(SPAN, stateAt(11)).markers)).toEqual(['a', 'b'])
    expect(uids(applyPlayhead(SPAN, stateAt(14)).markers)).toEqual(['a', 'b', 'c', 'd'])
  })

  it('shows only the trailing twelve months in window12', () => {
    const window = { mode: 'window12' as const }
    expect(uids(applyPlayhead(SPAN, stateAt(14, window)).markers)).toEqual(['b', 'c', 'd'])
    expect(uids(applyPlayhead(SPAN, stateAt(12, window)).markers)).toEqual(['b', 'c'])
    expect(uids(applyPlayhead(SPAN, stateAt(5, window)).markers)).toEqual(['a', 'b'])
  })

  it('reports the markers that are new since the previous state as the landing set', () => {
    expect(applyPlayhead(SPAN, stateAt(5), { previous: stateAt(4) }).landing).toEqual(['b'])
    expect(applyPlayhead(SPAN, stateAt(4), { previous: stateAt(3) }).landing).toEqual([])
    expect(
      applyPlayhead(SPAN, stateAt(12, { mode: 'window12' }), {
        previous: stateAt(5, { mode: 'window12' }),
      }).landing,
    ).toEqual(['c'])
  })

  it('lands nothing when there is no previous state', () => {
    expect(applyPlayhead(SPAN, stateAt(14)).landing).toEqual([])
  })

  it('keeps an undated marker visible in both modes and never lands it', () => {
    const markers = [...SPAN, marker('undated', 'not-a-date')]
    expect(uids(applyPlayhead(markers, stateAt(0)).markers)).toEqual(['a', 'undated'])
    expect(uids(applyPlayhead(markers, stateAt(14, { mode: 'window12' })).markers)).toEqual([
      'b',
      'c',
      'd',
      'undated',
    ])
    expect(applyPlayhead(markers, stateAt(1), { previous: stateAt(0) }).landing).toEqual([])
  })

  it('accepts a precomputed histogram and answers identically', () => {
    expect(uids(applyPlayhead(SPAN, stateAt(5), { histogram: SPAN_HISTOGRAM }).markers)).toEqual(
      uids(applyPlayhead(SPAN, stateAt(5)).markers),
    )
  })
})

describe('keyboard steps', () => {
  it('toggles playback on space', () => {
    expect(actionForKey(' ', stateAt(3))).toEqual({ type: 'play' })
    expect(actionForKey(' ', stateAt(3, { playing: true }))).toEqual({ type: 'pause' })
    expect(actionForKey('Spacebar', stateAt(3))).toEqual({ type: 'play' })
  })

  it('steps one bucket per arrow key', () => {
    expect(actionForKey('ArrowRight', stateAt(3))).toEqual({ type: 'seek', index: 4 })
    expect(actionForKey('ArrowLeft', stateAt(3))).toEqual({ type: 'seek', index: 2 })
  })

  it('ignores any other key', () => {
    expect(actionForKey('Enter', stateAt(3))).toBeNull()
    expect(actionForKey('a', stateAt(3))).toBeNull()
  })
})

describe('nextSpeed', () => {
  it('cycles the phone chip 1x - 2x - 4x - 1x', () => {
    expect(nextSpeed(1)).toBe(2)
    expect(nextSpeed(2)).toBe(4)
    expect(nextSpeed(4)).toBe(1)
  })
})
