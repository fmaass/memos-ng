import { describe, expect, it } from 'vitest'

import { toFeatureCollection } from '@/lib/places/geo'
import {
  installMarkerSource,
  MARKERS_PROBE_LAYER,
  MARKERS_SOURCE,
  type MarkerSourceHost,
} from '@/lib/places/source'

import { marker } from './fixtures'

/** A stand-in for the MapLibre map: only the four calls the installer makes. Not the unit. */
function fakeMap() {
  const sources = new Map<string, unknown>()
  const layers = new Map<string, unknown>()
  const calls: string[] = []
  const host = {
    getSource: (id: string) => sources.get(id),
    getLayer: (id: string) => layers.get(id),
    addSource: (id: string, spec: unknown) => {
      calls.push(`addSource:${id}`)
      sources.set(id, spec)
    },
    addLayer: (spec: { id: string }) => {
      calls.push(`addLayer:${spec.id}`)
      layers.set(spec.id, spec)
    },
  } as unknown as MarkerSourceHost
  /** What `setStyle` does to a map: the whole style, sources and layers included, is replaced. */
  const swapStyle = () => {
    sources.clear()
    layers.clear()
  }
  return { host, sources, layers, calls, swapStyle }
}

const data = toFeatureCollection([marker()])

describe('installMarkerSource', () => {
  it('installs the clustering source and the probe layer that makes it load tiles', () => {
    const map = fakeMap()
    expect(installMarkerSource(map.host, data)).toBe(true)
    expect(map.calls).toEqual([`addSource:${MARKERS_SOURCE}`, `addLayer:${MARKERS_PROBE_LAYER}`])
    expect(map.sources.get(MARKERS_SOURCE)).toMatchObject({ type: 'geojson', cluster: true })
  })

  it('is idempotent: a map that already has them is left alone', () => {
    const map = fakeMap()
    installMarkerSource(map.host, data)
    map.calls.length = 0
    expect(installMarkerSource(map.host, data)).toBe(false)
    expect(map.calls).toEqual([])
  })

  it('re-installs after a style swap, which is what a theme toggle does to the map', () => {
    // MapLibre's style diff drops a source the new style does not declare, and `load` does not
    // fire a second time — without this the pins go stale and cluster taps find no leaves.
    const map = fakeMap()
    installMarkerSource(map.host, data)
    map.swapStyle()
    map.calls.length = 0
    expect(installMarkerSource(map.host, data)).toBe(true)
    expect(map.calls).toEqual([`addSource:${MARKERS_SOURCE}`, `addLayer:${MARKERS_PROBE_LAYER}`])
  })
})
