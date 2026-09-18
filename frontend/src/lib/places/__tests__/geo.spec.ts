import { describe, expect, it } from 'vitest'

import {
  bboxOf,
  coordKey,
  FIT_PADDING,
  fitPadding,
  groupByCoordinate,
  MIN_BBOX_SPAN,
  toFeatureCollection,
} from '@/lib/places/geo'

import { marker } from './fixtures'

describe('toFeatureCollection', () => {
  it('is an empty collection for no markers', () => {
    expect(toFeatureCollection([])).toEqual({ type: 'FeatureCollection', features: [] })
  })

  it('carries only uid and taken-at millis into the feature payload', () => {
    // The clustering source ships these features to a web worker on every setData; the
    // AttachmentRef fields (signed URLs, blurhash) stay in the view's own Map<uid, marker>.
    const [feature] = toFeatureCollection([
      marker({ uid: 'a', latitude: 12.5, longitude: -3.25, taken_at: '2026-01-02T03:04:05Z' }),
    ]).features

    expect(feature?.geometry).toEqual({ type: 'Point', coordinates: [-3.25, 12.5] })
    expect(feature?.properties).toEqual({ uid: 'a', t: Date.parse('2026-01-02T03:04:05Z') })
  })
})

describe('bboxOf', () => {
  it('is null for no markers (there is nothing to fit to)', () => {
    expect(bboxOf([])).toBeNull()
  })

  it('pads a single pin into a box fitBounds can use', () => {
    // fitBounds on a zero-area box has no scale to solve for; a fixed minimum span keeps the
    // first view at the pin instead of at an accidental zoom.
    const box = bboxOf([marker({ latitude: 47.2, longitude: 8.4 })])

    expect(box).not.toBeNull()
    const [west, south, east, north] = box!
    expect(east - west).toBeCloseTo(MIN_BBOX_SPAN, 10)
    expect(north - south).toBeCloseTo(MIN_BBOX_SPAN, 10)
    expect((east + west) / 2).toBeCloseTo(8.4, 10)
    expect((north + south) / 2).toBeCloseTo(47.2, 10)
  })

  it('spans every pin when they are spread out', () => {
    const box = bboxOf([
      marker({ latitude: 47.2, longitude: 8.4 }),
      marker({ latitude: 47.6, longitude: 8.9 }),
      marker({ latitude: 47.4, longitude: 8.1 }),
    ])

    expect(box).toEqual([8.1, 47.2, 8.9, 47.6])
  })

  it('pads only the degenerate axis when every pin shares one coordinate', () => {
    const box = bboxOf([
      marker({ latitude: 47.2, longitude: 8.4 }),
      marker({ latitude: 47.6, longitude: 8.4 }),
    ])!

    expect(box[2] - box[0]).toBeCloseTo(MIN_BBOX_SPAN, 10)
    expect(box[1]).toBe(47.2)
    expect(box[3]).toBe(47.6)
  })
})

describe('groupByCoordinate', () => {
  it('collapses identical coordinates into one group, newest-first order preserved', () => {
    const groups = groupByCoordinate([
      marker({ uid: 'a', latitude: 47.2, longitude: 8.4 }),
      marker({ uid: 'b', latitude: 47.2, longitude: 8.4 }),
      marker({ uid: 'c', latitude: 47.6, longitude: 8.9 }),
    ])

    expect(groups).toHaveLength(2)
    expect(groups[0]?.markers.map((m) => m.uid)).toEqual(['a', 'b'])
    expect(groups[0]?.key).toBe(coordKey(47.2, 8.4))
    expect(groups[1]?.markers.map((m) => m.uid)).toEqual(['c'])
  })

  it('keeps coordinates apart below the rounding precision', () => {
    const groups = groupByCoordinate([
      marker({ uid: 'a', latitude: 47.2, longitude: 8.4 }),
      marker({ uid: 'b', latitude: 47.2000004, longitude: 8.4 }),
      marker({ uid: 'c', latitude: 47.2001, longitude: 8.4 }),
    ])

    // 7th decimal is ~1 cm: the same spot. The 4th decimal is ~10 m: two spots.
    expect(groups).toHaveLength(2)
    expect(groups[0]?.markers.map((m) => m.uid)).toEqual(['a', 'b'])
  })
})

describe('fitPadding', () => {
  // The bar is an absolutely-positioned overlay on the map pane, so the pixels it covers are
  // pixels `fitBounds` will happily put a pin in. Live at 1440x900 that put 2 of 8 pins at
  // y 826 behind a bar occupying y 812-884, where nothing can click them.
  const pane = { top: 56, bottom: 900, left: 240, right: 1440, height: 844 }

  it('reserves the overlay it is given, on the bottom side only', () => {
    const bar = { top: 812, bottom: 884, left: 256, right: 1424, height: 72 }

    const padding = fitPadding(pane, bar)

    expect(padding.bottom).toBe(FIT_PADDING + (pane.bottom - bar.top))
    expect(padding.top).toBe(FIT_PADDING)
    expect(padding.left).toBe(FIT_PADDING)
    expect(padding.right).toBe(FIT_PADDING)
  })

  it('reserves what a taller or a shorter overlay actually occupies, not a fixed number', () => {
    // The bar is a different height on a phone (smaller type, tighter insets) and grows again
    // when its controls wrap, so the reservation is measured on every fit.
    const shallow = fitPadding(pane, { top: 852, bottom: 892, left: 8, right: 382, height: 40 })
    const deep = fitPadding(pane, { top: 760, bottom: 884, left: 8, right: 382, height: 124 })

    expect(deep.bottom).toBeGreaterThan(shallow.bottom)
    expect(deep.bottom - shallow.bottom).toBe(852 - 760)
  })

  it('falls back to the uniform padding when no overlay is on the pane', () => {
    // No markers means no timeline bar at all (PlacesView renders it `v-if="markers.length"`).
    expect(fitPadding(pane, null)).toEqual({
      top: FIT_PADDING,
      right: FIT_PADDING,
      bottom: FIT_PADDING,
      left: FIT_PADDING,
    })
  })

  it('ignores an overlay that sits clear of the pane', () => {
    // On a phone the in-view grid is BELOW the map rather than over it: it hides no pin and
    // must reserve nothing, or the fit would throw away half the short pane for nothing.
    const below = { top: 950, bottom: 1400, left: 0, right: 390, height: 450 }

    expect(fitPadding(pane, below).bottom).toBe(FIT_PADDING)
  })

  it('ignores an overlay entirely above the pane', () => {
    // A banner over the header shares no pixel with the map: reserving for it would throw away
    // the bottom of the pane for something that hides nothing.
    const above = { top: 0, bottom: 40, left: 240, right: 1440, height: 40 }

    expect(fitPadding(pane, above).bottom).toBe(FIT_PADDING)
  })

  it('ignores an overlay that misses the pane horizontally', () => {
    // A side panel beside the map, at the same height: it overlaps on the y axis and covers no
    // pin at all, so the reservation has to test both axes.
    const beside = { top: 700, bottom: 900, left: 0, right: 239, height: 200 }

    expect(fitPadding(pane, beside).bottom).toBe(FIT_PADDING)
  })

  it('reserves nothing for an overlay anchored at the TOP of the pane', () => {
    // The contract is deliberately narrow: this reserves for a BOTTOM-anchored overlay only.
    // A top bar overlapping the pane would need the top side reserved instead, and guessing
    // that from a rectangle would silently mis-fit the one case the map actually has.
    const topBar = { top: 40, bottom: 120, left: 240, right: 1440, height: 80 }

    expect(fitPadding(pane, topBar)).toEqual({
      top: FIT_PADDING,
      right: FIT_PADDING,
      bottom: FIT_PADDING,
      left: FIT_PADDING,
    })
  })

  it('never lets the reservation swallow the pane', () => {
    // A short pane with a tall bar: padding that exceeds the pane leaves `fitBounds` no box to
    // solve for, and the first view lands on nothing instead of on the pins.
    const short = { top: 56, bottom: 520, left: 0, right: 390, height: 464 }
    const tall = { top: 100, bottom: 512, left: 8, right: 382, height: 412 }

    const padding = fitPadding(short, tall)

    expect(padding.top + padding.bottom).toBeLessThan(short.height)
    expect(padding.bottom).toBeGreaterThan(0)
  })
})
