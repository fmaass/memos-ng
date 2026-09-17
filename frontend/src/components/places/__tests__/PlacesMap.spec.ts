import { flushPromises, mount } from '@vue/test-utils'
import type { FeatureCollection, Point } from 'geojson'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { marker } from '@/lib/places/__tests__/fixtures'
import type { MarkerProps } from '@/lib/places/geo'
import { MARKERS_SOURCE } from '@/lib/places/source'

// MapLibre needs a WebGL canvas, which happy-dom has not got — so the map itself is a double and
// the UNIT under test is PlacesMap's wiring: what it hands the source, what it re-installs after a
// style swap, and which pins it reports as in view.
vi.mock('maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url', () => ({ default: '/worker.js' }))
vi.mock('@/composables/useApi', () => ({
  useApi: () => ({ GET: async () => ({ data: { build: null, planet: null, detail: null } }) }),
}))

type Data = FeatureCollection<Point, MarkerProps>

const maplibre = vi.hoisted(() => {
  const state = {
    map: null as FakeMap | null,
    elements: [] as { element: HTMLElement; lngLat: [number, number] }[],
  }

  class FakeSource {
    constructor(public data: Data) {}
    setData(data: Data) {
      this.data = data
    }
  }

  class FakeMap {
    sources = new Map<string, FakeSource>()
    layers = new Map<string, unknown>()
    handlers = new Map<string, (() => void)[]>()
    styleSwaps = 0
    constructor() {
      state.map = this
    }
    on(event: string, handler: () => void) {
      this.handlers.set(event, [...(this.handlers.get(event) ?? []), handler])
    }
    fire(event: string) {
      for (const handler of this.handlers.get(event) ?? []) handler()
    }
    addControl() {}
    getSource(id: string) {
      return this.sources.get(id)
    }
    getLayer(id: string) {
      return this.layers.get(id)
    }
    addSource(id: string, spec: { data: Data }) {
      this.sources.set(id, new FakeSource(spec.data))
    }
    addLayer(spec: { id: string }) {
      this.layers.set(spec.id, spec)
    }
    /** What the app sees of the source: every feature currently in it. */
    querySourceFeatures(id: string) {
      return (this.sources.get(id)?.data.features ?? []).map((feature) => ({
        geometry: feature.geometry,
        properties: { uid: feature.properties.uid, t: feature.properties.t },
      }))
    }
    getCenter() {
      return { lng: 0, lat: 0 }
    }
    getBounds() {
      return { contains: () => true }
    }
    fitBounds() {}
    jumpTo() {}
    easeTo() {}
    /** A theme toggle: MapLibre's style diff drops every source and layer the new style lacks. */
    setStyle() {
      this.sources.clear()
      this.layers.clear()
      this.styleSwaps += 1
      this.fire('styledata')
    }
    remove() {}
  }

  class FakeMarker {
    element: HTMLElement
    constructor(options: { element: HTMLElement }) {
      this.element = options.element
    }
    setLngLat(lngLat: [number, number]) {
      state.elements.push({ element: this.element, lngLat })
      return this
    }
    addTo() {
      return this
    }
    remove() {}
  }

  return { state, FakeMap, FakeMarker }
})

vi.mock('maplibre-gl', () => ({
  addProtocol: () => {},
  setWorkerUrl: () => {},
  AttributionControl: class {},
  NavigationControl: class {},
  Map: maplibre.FakeMap,
  Marker: maplibre.FakeMarker,
}))
vi.mock('pmtiles', () => ({ Protocol: class {} }))

const { default: PlacesMap } = await import('@/components/places/PlacesMap.vue')

const jan = marker({
  uid: 'jan',
  taken_at: '2025-01-10T12:00:00Z',
  latitude: 47.1,
  longitude: 8.1,
})
const feb = marker({
  uid: 'feb',
  taken_at: '2025-02-10T12:00:00Z',
  latitude: 47.9,
  longitude: 8.9,
})

function uidsIn(data: Data | undefined): string[] {
  return (data?.features ?? []).map((feature) => feature.properties.uid)
}

async function mountMap() {
  const wrapper = mount(PlacesMap, { props: { markers: [jan, feb] } })
  await flushPromises()
  maplibre.state.map?.fire('load')
  return wrapper
}

beforeEach(() => {
  maplibre.state.map = null
  maplibre.state.elements = []
})

describe('PlacesMap timeline seam', () => {
  it('feeds the source exactly the set the playhead exposes', async () => {
    const wrapper = await mountMap()
    ;(wrapper.vm as unknown as { setVisibleMarkers: (m: unknown[]) => void }).setVisibleMarkers([
      jan,
    ])
    expect(uidsIn(maplibre.state.map?.sources.get(MARKERS_SOURCE)?.data)).toEqual(['jan'])
    // The per-tick cost the plan asks to be measured is read off the DOM, as Phase 3's sync is.
    expect(wrapper.attributes('data-set-data-ms')).toMatch(/^\d+$/)
    wrapper.unmount()
  })

  it('puts the playhead set back after a theme swap, not every pin', async () => {
    // The re-install on `styledata` is what survives `setStyle`; feeding it the props would
    // silently restore every pin mid-playback and the map would jump to the end of the timeline.
    const wrapper = await mountMap()
    ;(wrapper.vm as unknown as { setVisibleMarkers: (m: unknown[]) => void }).setVisibleMarkers([
      jan,
    ])
    maplibre.state.map?.setStyle()
    expect(maplibre.state.map?.styleSwaps).toBe(1)
    expect(uidsIn(maplibre.state.map?.sources.get(MARKERS_SOURCE)?.data)).toEqual(['jan'])
    wrapper.unmount()
  })

  it('reports as in view only the pins the playhead exposes', async () => {
    const wrapper = await mountMap()
    ;(wrapper.vm as unknown as { setVisibleMarkers: (m: unknown[]) => void }).setVisibleMarkers([
      jan,
    ])
    expect(wrapper.emitted('visible-change')?.at(-1)).toEqual([['jan']])
    wrapper.unmount()
  })

  it('marks the pins that arrived in this tick so they can land', async () => {
    const wrapper = await mountMap()
    const vm = wrapper.vm as unknown as {
      setVisibleMarkers: (m: unknown[], landing?: string[]) => void
    }
    vm.setVisibleMarkers([jan])
    const standing = maplibre.state.elements.find(({ lngLat }) => lngLat[0] === 8.1)?.element
    maplibre.state.elements = []
    vm.setVisibleMarkers([jan, feb], ['feb'])
    const landed = maplibre.state.elements.find(({ lngLat }) => lngLat[0] === 8.9)?.element
    expect(landed?.classList.contains('places-pin--landing')).toBe(true)
    // The pin that was already on the map does not land a second time.
    expect(standing?.classList.contains('places-pin--landing')).toBe(false)
    wrapper.unmount()
  })
})
