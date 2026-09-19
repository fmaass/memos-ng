import { describe, expect, it } from 'vitest'

import {
  bboxOf,
  coordKey,
  groupByCoordinate,
  MIN_BBOX_SPAN,
  toFeatureCollection,
} from '@/lib/places/geo'

import { marker } from './fixtures'

describe('toFeatureCollection', () => {
  it('is an empty collection for no markers', () => {
    expect(toFeatureCollection([])).toEqual({ type: 'FeatureCollection', features: [] })
  })

  it('carries only the uid into the feature payload', () => {
    // The clustering source ships these features to a web worker on every setData; the
    // AttachmentRef fields (signed URLs, blurhash) stay in the view's own Map<uid, marker>.
    const [feature] = toFeatureCollection([
      marker({ uid: 'a', latitude: 12.5, longitude: -3.25, taken_at: '2026-01-02T03:04:05Z' }),
    ]).features

    expect(feature?.geometry).toEqual({ type: 'Point', coordinates: [-3.25, 12.5] })
    expect(feature?.properties).toEqual({ uid: 'a' })
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

describe('coordKey', () => {
  // The native client rounds the same coordinates to build the same pin key, so these vectors are
  // the parity contract between the two clients: every expected value is JavaScript's own
  // `toFixed(6)` — rounding of the EXACT binary value, half away from zero — confirmed on this
  // host before it was written down. Each vector runs through both halves of the key, because a
  // rounding change in either half must be caught.
  const vectors: [value: number, rounded: string][] = [
    [0.0078125, '0.007813'], // an exact tie in binary: it rounds away from zero, not to even
    [1.0000005, '1.000001'],
    [0.0000015, '0.000002'],
    [46.9479745, '46.947975'],
    [179.9999995, '180.000000'],
    [47.3769875, '47.376987'], // stored just BELOW the decimal tie, so it rounds DOWN
    [8.0000015, '8.000001'],
    [8.5417235, '8.541723'],
    [100.0000005, '100.000000'],
  ]

  it.each(vectors)('rounds %s to %s in both halves of the key', (value, rounded) => {
    expect(coordKey(value, value)).toBe(`${rounded},${rounded}`)
  })

  it('writes no negative zero', () => {
    // A coordinate that reaches zero from the west and one that reaches it from the east are one
    // spot; a "-0.000000" half would split them into two pins that can never merge.
    expect(coordKey(-0, -0)).toBe('0.000000,0.000000')
    expect(coordKey(-0, -0)).not.toContain('-')
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
