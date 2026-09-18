import { flushPromises, mount } from '@vue/test-utils'
import type { FeatureCollection, Point } from 'geojson'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { marker } from '@/lib/places/__tests__/fixtures'
import { FIT_PADDING, type MarkerProps } from '@/lib/places/geo'
import { MARKERS_SOURCE } from '@/lib/places/source'

// MapLibre needs a WebGL canvas, which happy-dom has not got — so the map itself is a double and
// the UNIT under test is PlacesMap's wiring: what it hands the source, what it re-installs after a
// style swap, and which pins it reports as in view.
vi.mock('maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url', () => ({ default: '/worker.js' }))
vi.mock('@/composables/useApi', () => ({
  useApi: () => ({ GET: async () => ({ data: { build: null, planet: null, detail: null } }) }),
}))

type Data = FeatureCollection<Point, MarkerProps>

/** A desktop map pane, stated once: the fit assertions read against these numbers. */
const PANE = { top: 56, bottom: 900, left: 240, right: 1440, height: 844 }

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
        properties: { uid: feature.properties.uid },
      }))
    }
    getCenter() {
      return { lng: 0, lat: 0 }
    }
    getBounds() {
      return { contains: () => true }
    }
    fits: { padding: unknown }[] = []
    fitBounds(_box: unknown, options: { padding: unknown }) {
      this.fits.push({ padding: options.padding })
    }
    getContainer() {
      return { getBoundingClientRect: () => ({ ...PANE, width: PANE.right - PANE.left }) }
    }
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

describe('PlacesMap data seam', () => {
  it('feeds the source exactly the markers it is given', async () => {
    const wrapper = await mountMap()
    await wrapper.setProps({ markers: [jan] })
    expect(uidsIn(maplibre.state.map?.sources.get(MARKERS_SOURCE)?.data)).toEqual(['jan'])
    wrapper.unmount()
  })

  it('puts the markers back after a theme swap', async () => {
    // The re-install on `styledata` is what survives `setStyle`: MapLibre's style diff drops the
    // source and the probe layer, and `load` never fires a second time.
    const wrapper = await mountMap()
    await wrapper.setProps({ markers: [jan] })
    maplibre.state.map?.setStyle()
    expect(maplibre.state.map?.styleSwaps).toBe(1)
    expect(uidsIn(maplibre.state.map?.sources.get(MARKERS_SOURCE)?.data)).toEqual(['jan'])
    wrapper.unmount()
  })

  it('reports as in view the pins the map currently holds', async () => {
    const wrapper = await mountMap()
    await wrapper.setProps({ markers: [jan] })
    expect(wrapper.emitted('visible-change')?.at(-1)).toEqual([['jan']])
    wrapper.unmount()
  })
})

describe('PlacesMap pin payload', () => {
  /** Three photos from one spot, so they collapse into a single badge pin. */
  const here = (uid: string) =>
    marker({ uid, taken_at: '2025-03-10T12:00:00Z', latitude: 47.5, longitude: 8.5 })

  it('hands the drawer the photos the pin now holds, not the ones it held', async () => {
    // A refreshed markers query after one photo was deleted and another arrived at the same spot:
    // the pin's lead, count and coordinate are unchanged, only its membership moved.
    const wrapper = mount(PlacesMap, { props: { markers: [here('a'), here('b')] } })
    await flushPromises()
    maplibre.state.map?.fire('load')

    await wrapper.setProps({ markers: [here('a'), here('c')] })
    const pin = maplibre.state.elements.filter(({ lngLat }) => lngLat[0] === 8.5).at(-1)?.element
    pin?.dispatchEvent(new Event('click'))
    await flushPromises()

    expect(wrapper.emitted('cluster-open')?.at(-1)).toEqual([{ clusterId: null, uids: ['a', 'c'] }])
    wrapper.unmount()
  })
})

describe('the first fit', () => {
  it('frames the pins with the uniform padding, reserving no band of the pane', async () => {
    const wrapper = mount(PlacesMap, { props: { markers: [jan, feb] } })
    await flushPromises()
    maplibre.state.map?.fire('load')
    await flushPromises()

    expect(maplibre.state.map?.fits.at(-1)?.padding).toEqual({
      top: FIT_PADDING,
      right: FIT_PADDING,
      bottom: FIT_PADDING,
      left: FIT_PADDING,
    })
    wrapper.unmount()
  })
})
