import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { defineComponent, h, nextTick, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { marker } from '@/lib/places/__tests__/fixtures'
import type { MapMarker } from '@/lib/places/geo'

// Three months apart, so a set that was filtered by anything would show it.
const jan = marker({ uid: 'jan', memo_uid: 'm-jan', taken_at: '2025-01-15T12:00:00Z' })
const feb = marker({ uid: 'feb', memo_uid: 'm-feb', taken_at: '2025-02-15T12:00:00Z' })
const mar = marker({ uid: 'mar', memo_uid: 'm-mar', taken_at: '2025-03-15T12:00:00Z' })
const ALL = [jan, feb, mar]

// The markers query, drivable: the view has to distinguish an unanswered request from an empty
// library, so a test must be able to hold it unanswered and then answer it.
const query = vi.hoisted(() => ({
  markers: [] as MapMarker[],
  state: null as null | { data: unknown; isPending: unknown; isError: unknown },
}))
vi.mock('@tanstack/vue-query', async () => {
  const { ref } = await import('vue')
  return {
    useQuery: () => {
      if (query.state) return query.state
      return { data: ref({ markers: query.markers }), isPending: ref(false), isError: ref(false) }
    },
  }
})
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

// The map arrives as its own chunk now, so the seam the view really has is the LOADER: hold it
// unanswered and the view has to paint anyway, reject it and the pane has to offer a way back.
const mapChunk = vi.hoisted(() => ({ load: null as null | (() => Promise<unknown>) }))
vi.mock('@/lib/places/loadMap', () => ({ loadPlacesMap: () => mapChunk.load!() }))

// PhotoSwipe cannot open in happy-dom; what this view owes it is the list it opens on.
const lightbox = vi.hoisted(() => ({
  open: vi.fn(),
  destroy: vi.fn(),
  refreshPrintMaster: vi.fn(),
}))
vi.mock('@/composables/useMediaLightbox', () => ({ useMediaLightbox: () => lightbox }))

const zoomToCluster = vi.fn()
/** The map, reduced to the seam the view drives: it keeps the marker set it was handed. */
const MapStub = defineComponent({
  name: 'PlacesMap',
  props: { markers: { type: Array, default: () => [] } },
  emits: ['cluster-open', 'point-open', 'visible-change'],
  setup(_props, { expose }) {
    expose({ zoomToCluster })
    return () => h('div', { 'data-test': 'map-stub' })
  },
})
const listStub = (name: string, testId: string) =>
  defineComponent({
    name,
    props: { markers: { type: Array, default: () => [] } },
    setup: (props) => () => h('div', { 'data-test': testId }, String(props.markers.length)),
  })

function uidsOf(list: unknown): string[] {
  return (list as MapMarker[]).map((m) => m.uid)
}
/** The set the map is showing: the view hands it over as a prop, nothing narrows it. */
function shownOnMap(wrapper: VueWrapper): string[] {
  return uidsOf(wrapper.findComponent(MapStub).props('markers'))
}

function mountPending(phone = false) {
  window.matchMedia = ((q: string) =>
    ({
      matches: phone,
      media: q,
      addEventListener: () => {},
      removeEventListener: () => {},
    }) as unknown as MediaQueryList) as typeof window.matchMedia
  return mount(PlacesView, {
    global: {
      plugins: [PrimeVue],
      stubs: {
        ClusterDrawer: listStub('ClusterDrawer', 'drawer-stub'),
        InViewGrid: listStub('InViewGrid', 'grid-stub'),
      },
    },
  })
}

/** The view with its map chunk delivered — the state every one of these tests is about. */
async function mountView(phone = false) {
  const wrapper = mountPending(phone)
  await flushPromises()
  return wrapper
}

// Imported after the mocks so the component picks them up.
const { default: PlacesView } = await import('@/views/PlacesView.vue')

beforeEach(() => {
  query.markers = ALL
  lightbox.open.mockClear()
  mapChunk.load = () => Promise.resolve({ default: MapStub, __esModule: true })
})

describe('PlacesView map wiring', () => {
  it('shows every located photo on the map, and no timeline bar', async () => {
    // The timeline was removed: nothing filters the marker set any more, and no bar is drawn
    // over the pane. Both halves have to hold — a bar without a filter would be just as wrong.
    const wrapper = await mountView()
    await wrapper.vm.$nextTick()
    expect(shownOnMap(wrapper)).toEqual(['jan', 'feb', 'mar'])
    expect(wrapper.find('[data-test="timeline-bar"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the drawer the photos behind the pin that was tapped', async () => {
    const wrapper = await mountView()
    await wrapper.vm.$nextTick()
    wrapper.findComponent(MapStub).vm.$emit('cluster-open', {
      clusterId: 7,
      uids: ['jan', 'feb', 'mar'],
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-test="drawer-stub"]').text()).toBe('3')
    wrapper.unmount()
  })

  it('shows the phone grid the photos the map reports in view', async () => {
    const wrapper = await mountView(true)
    await wrapper.vm.$nextTick()
    wrapper.findComponent(MapStub).vm.$emit('visible-change', ['jan', 'feb'])
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-test="grid-stub"]').text()).toBe('2')
    wrapper.unmount()
  })

  it('opens the viewer on the photos in view when a single pin is tapped', async () => {
    const wrapper = await mountView()
    await wrapper.vm.$nextTick()
    wrapper.findComponent(MapStub).vm.$emit('visible-change', ['jan', 'feb', 'mar'])
    await wrapper.vm.$nextTick()
    wrapper.findComponent(MapStub).vm.$emit('point-open', 'feb')
    await wrapper.vm.$nextTick()

    expect(lightbox.open).toHaveBeenCalledTimes(1)
    const [items, index] = lightbox.open.mock.calls[0] as [unknown[], number]
    // Newest first (`viewerTarget`): mar, feb, jan — the tapped photo is the middle one.
    expect(items).toHaveLength(3)
    expect(index).toBe(1)
    wrapper.unmount()
  })
})

describe('what the map says before the markers arrive', () => {
  // 1.7.0 rendered "No photo here knows where it was taken yet" from the absence of markers
  // alone, so it claimed it while the 1.1 MB markers request was still in flight — the feature
  // read as "found nothing" for the first seconds of every cold open.
  async function mountWith(state: {
    markers?: MapMarker[]
    isPending: boolean
    isError: boolean
  }) {
    const data = ref(state.markers ? { markers: state.markers } : undefined)
    const isPending = ref(state.isPending)
    const isError = ref(state.isError)
    query.state = { data, isPending, isError }
    const wrapper = await mountView()
    return { wrapper, data, isPending, isError }
  }

  afterEach(() => {
    query.state = null
  })

  it('says nothing about an empty library while the request is still in flight', async () => {
    const { wrapper } = await mountWith({ isPending: true, isError: false })

    expect(wrapper.find('[data-test="places-empty"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="places-loading"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('tells a failed request apart from an empty library', async () => {
    const { wrapper } = await mountWith({ markers: [], isPending: false, isError: true })

    expect(wrapper.find('[data-test="places-empty"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="places-error"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('says the library is empty once the query has settled with nothing', async () => {
    const { wrapper } = await mountWith({ markers: [], isPending: false, isError: false })

    expect(wrapper.find('[data-test="places-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="places-error"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the pins, and no message, when markers arrive LATE', async () => {
    // The map is mounted before the data exists — the sequence every cold open takes. The empty
    // state may not appear at any point of it.
    const { wrapper, data, isPending } = await mountWith({ isPending: true, isError: false })
    expect(wrapper.find('[data-test="places-empty"]').exists()).toBe(false)

    data.value = { markers: ALL }
    isPending.value = false
    await nextTick()

    expect(wrapper.find('[data-test="places-empty"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="places-error"]').exists()).toBe(false)
    expect(shownOnMap(wrapper)).toEqual(['jan', 'feb', 'mar'])
    wrapper.unmount()
  })
})

describe('the view does not wait for the map chunk', () => {
  it('paints the shell and says the map is on its way', async () => {
    // The defect: `/places` is a 12 KB route, but it used to import the 1.09 MB map statically,
    // so the navigation itself could not finish until the whole map had come over the tunnel —
    // and nothing on screen said so.
    mapChunk.load = () => new Promise(() => {})
    const wrapper = mountPending()

    expect(wrapper.find('[data-test="view-places"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="map-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="map-stub"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('asks for the chunk again when the pane offers a retry', async () => {
    let attempts = 0
    mapChunk.load = () => {
      attempts += 1
      return attempts === 1
        ? Promise.reject(new Error('chunk load failed'))
        : Promise.resolve({ default: MapStub, __esModule: true })
    }
    const wrapper = mountPending()
    await flushPromises()
    expect(wrapper.find('[data-test="map-load-error"]').exists()).toBe(true)

    await wrapper.find('[data-test="map-retry"]').trigger('click')
    await flushPromises()

    expect(attempts).toBe(2)
    expect(wrapper.find('[data-test="map-stub"]').exists()).toBe(true)
    wrapper.unmount()
  })
})
