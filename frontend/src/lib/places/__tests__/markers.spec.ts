import { describe, expect, it } from 'vitest'

import {
  badgeText,
  MAX_DOM_MARKERS,
  type PinSpec,
  pinsFor,
  type SourceFeature,
  syncMarkers,
} from '@/lib/places/markers'

function clusterFeature(id: number, count: number, lng: number, lat: number, uid = `u${id}`): SourceFeature {
  return {
    geometry: { type: 'Point', coordinates: [lng, lat] },
    properties: { cluster: true, cluster_id: id, point_count: count, uid },
  }
}

function pointFeature(uid: string, lng: number, lat: number): SourceFeature {
  return { geometry: { type: 'Point', coordinates: [lng, lat] }, properties: { uid, t: 0 } }
}

describe('badgeText', () => {
  it('hides the badge for a single photo', () => {
    // A "1" on every lone pin is noise: the thumbnail already says one photo.
    expect(badgeText(1)).toBeNull()
    expect(badgeText(0)).toBeNull()
  })

  it('shows the exact count up to 99', () => {
    expect(badgeText(2)).toBe('2')
    expect(badgeText(99)).toBe('99')
  })

  it('caps at 99+ so the badge stays inside the pin', () => {
    expect(badgeText(100)).toBe('99+')
    expect(badgeText(4213)).toBe('99+')
  })
})

describe('pinsFor', () => {
  it('turns a cluster feature into one pin carrying its id and count', () => {
    const [pin] = pinsFor([clusterFeature(7, 42, 8.4, 47.2, 'lead')], { center: [8.4, 47.2] })

    expect(pin).toMatchObject({ key: 'c:7', clusterId: 7, count: 42, uid: 'lead', lng: 8.4, lat: 47.2 })
  })

  it('collapses points at identical coordinates into one badge pin', () => {
    // Above clusterMaxZoom MapLibre stops clustering, so two photos from the same spot would
    // otherwise stack into one unreadable, untappable pin.
    const pins = pinsFor(
      [pointFeature('a', 8.4, 47.2), pointFeature('b', 8.4, 47.2), pointFeature('c', 8.9, 47.6)],
      { center: [8.4, 47.2] },
    )

    expect(pins).toHaveLength(2)
    const collapsed = pins.find((p) => p.count === 2)
    expect(collapsed?.clusterId).toBeNull()
    expect(collapsed?.uid).toBe('a')
    expect(collapsed?.uids).toEqual(['a', 'b'])
  })

  it('draws one pin per cluster and per photo when tiles repeat a feature', () => {
    // querySourceFeatures answers from every loaded tile, and a cluster on a tile boundary (or a
    // point still cached at another zoom) comes back more than once. Counted twice, a cluster of
    // 12 reads as 24 photos and the map grows a second DOM marker nothing ever removes.
    const pins = pinsFor(
      [
        clusterFeature(7, 12, 8.4, 47.2, 'lead'),
        clusterFeature(7, 12, 8.4, 47.2, 'lead'),
        pointFeature('a', 8.9, 47.6),
        pointFeature('a', 8.9, 47.6),
      ],
      { center: [8.4, 47.2] },
    )

    expect(pins).toHaveLength(2)
    expect(pins.map((p) => p.count)).toEqual([12, 1])
    expect(pins[1]?.uids).toEqual(['a'])
  })

  it('caps the DOM markers at the ones nearest the viewport centre', () => {
    const features = Array.from({ length: MAX_DOM_MARKERS + 40 }, (_, i) =>
      pointFeature(`u${i}`, 8.4 + i / 1000, 47.2),
    )

    const pins = pinsFor(features, { center: [8.4, 47.2] })

    expect(pins).toHaveLength(MAX_DOM_MARKERS)
    expect(pins.some((p) => p.uid === 'u0')).toBe(true)
    expect(pins.some((p) => p.uid === `u${MAX_DOM_MARKERS + 39}`)).toBe(false)
  })

  it('honours an explicit cap', () => {
    const features = [pointFeature('near', 8.4, 47.2), pointFeature('far', 20, 47.2)]

    expect(pinsFor(features, { center: [8.4, 47.2], cap: 1 }).map((p) => p.uid)).toEqual(['near'])
  })
})

describe('syncMarkers', () => {
  const a: PinSpec = { key: 'c:1', lng: 1, lat: 1, count: 3, uid: 'a', clusterId: 1, uids: [] }
  const b: PinSpec = { key: 'p:2', lng: 2, lat: 2, count: 1, uid: 'b', clusterId: null, uids: ['b'] }

  it('adds every pin when nothing is on the map yet', () => {
    const diff = syncMarkers(new Map(), [a, b])

    expect(diff.add).toEqual([a, b])
    expect(diff.update).toEqual([])
    expect(diff.remove).toEqual([])
  })

  it('removes the pins the new set no longer holds', () => {
    const diff = syncMarkers(new Map([['c:1', a], ['p:2', b]]), [a])

    expect(diff.remove).toEqual(['p:2'])
    expect(diff.add).toEqual([])
  })

  it('updates a pin whose count, lead photo or position moved, and leaves an identical one alone', () => {
    // A cluster id survives a zoom change while its count and centroid do not — repainting only
    // what changed is what keeps the sync inside one frame.
    const moved = { ...a, count: 9, lng: 1.5 }
    const diff = syncMarkers(new Map([['c:1', a], ['p:2', b]]), [moved, b])

    expect(diff.update).toEqual([moved])
    expect(diff.add).toEqual([])
    expect(diff.remove).toEqual([])
  })
})
