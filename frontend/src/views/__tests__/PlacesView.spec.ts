import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { defineComponent, h } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { marker } from '@/lib/places/__tests__/fixtures'
import type { MapMarker } from '@/lib/places/geo'

// Three months apart so every bucket boundary in the assertions is a real calendar month in
// Europe/Zurich, and the middle one proves the playhead is not just "all or nothing".
const jan = marker({ uid: 'jan', memo_uid: 'm-jan', taken_at: '2025-01-15T12:00:00Z' })
const feb = marker({ uid: 'feb', memo_uid: 'm-feb', taken_at: '2025-02-15T12:00:00Z' })
const mar = marker({ uid: 'mar', memo_uid: 'm-mar', taken_at: '2025-03-15T12:00:00Z' })
const ALL = [jan, feb, mar]

const query = vi.hoisted(() => ({ markers: [] as MapMarker[] }))
vi.mock('@tanstack/vue-query', async () => {
  const { ref } = await import('vue')
  return { useQuery: () => ({ data: ref({ markers: query.markers }), isPending: ref(false) }) }
})
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

const setVisibleMarkers = vi.fn()
const zoomToCluster = vi.fn()
/** The map, reduced to the seam the view drives: it records what it was handed per tick. */
const MapStub = defineComponent({
  name: 'PlacesMap',
  props: { markers: { type: Array, default: () => [] } },
  emits: ['cluster-open', 'point-open', 'visible-change'],
  setup(_props, { expose }) {
    expose({ setVisibleMarkers, zoomToCluster })
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
/** The set the map was last told to show. */
function lastShown(): string[] {
  const call = setVisibleMarkers.mock.calls.at(-1)
  return call ? uidsOf(call[0]) : []
}

function mountView(phone = false) {
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
        PlacesMap: MapStub,
        ClusterDrawer: listStub('ClusterDrawer', 'drawer-stub'),
        InViewGrid: listStub('InViewGrid', 'grid-stub'),
      },
    },
  })
}

// Imported after the mocks so the component picks them up.
const { default: PlacesView } = await import('@/views/PlacesView.vue')

function press(key: string, target: EventTarget = window) {
  target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }))
}

beforeEach(() => {
  query.markers = ALL
  setVisibleMarkers.mockClear()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('PlacesView timeline wiring', () => {
  it('opens showing every pin: the playhead parks at the newest bucket', async () => {
    const wrapper = mountView()
    await wrapper.vm.$nextTick()
    expect(lastShown()).toEqual(['jan', 'feb', 'mar'])
    wrapper.unmount()
  })

  it('steps back a bucket on ArrowLeft and hands the map the narrower set', async () => {
    const wrapper = mountView()
    await wrapper.vm.$nextTick()
    press('ArrowLeft')
    await wrapper.vm.$nextTick()
    expect(lastShown()).toEqual(['jan', 'feb'])
    wrapper.unmount()
  })

  it('shows the drawer only the photos the playhead exposes', async () => {
    const wrapper = mountView()
    await wrapper.vm.$nextTick()
    // A cluster tap names every leaf under the pin — including ones the playhead hides, because
    // MapLibre keeps tiles it has already clustered. The drawer must still read the filtered set.
    wrapper.findComponent(MapStub).vm.$emit('cluster-open', {
      clusterId: 7,
      uids: ['jan', 'feb', 'mar'],
    })
    press('ArrowLeft')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-test="drawer-stub"]').text()).toBe('2')
    wrapper.unmount()
  })

  it('shows the phone grid only the photos the playhead exposes', async () => {
    const wrapper = mountView(true)
    await wrapper.vm.$nextTick()
    wrapper.findComponent(MapStub).vm.$emit('visible-change', ['jan', 'feb', 'mar'])
    press('ArrowLeft')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-test="grid-stub"]').text()).toBe('2')
    wrapper.unmount()
  })

  it('plays from the start and lands one bucket per tick, in date order', async () => {
    vi.useFakeTimers()
    const wrapper = mountView()
    await wrapper.vm.$nextTick()
    press(' ')
    await wrapper.vm.$nextTick()
    // Play from the parked end means replay from the first bucket, not "advance nowhere".
    expect(lastShown()).toEqual(['jan'])
    vi.advanceTimersByTime(600)
    await wrapper.vm.$nextTick()
    expect(lastShown()).toEqual(['jan', 'feb'])
    vi.advanceTimersByTime(600)
    await wrapper.vm.$nextTick()
    expect(lastShown()).toEqual(['jan', 'feb', 'mar'])
    // The end pauses: no further ticks change anything.
    vi.advanceTimersByTime(3000)
    await wrapper.vm.$nextTick()
    expect(lastShown()).toEqual(['jan', 'feb', 'mar'])
    wrapper.unmount()
  })

  it('tells the map which pins are new in the tick, so they can land', async () => {
    vi.useFakeTimers()
    const wrapper = mountView()
    await wrapper.vm.$nextTick()
    press(' ')
    await wrapper.vm.$nextTick() // the ticker starts on the state change, not on the keystroke
    vi.advanceTimersByTime(600)
    await wrapper.vm.$nextTick()
    expect(setVisibleMarkers.mock.calls.at(-1)?.[1]).toEqual(['feb'])
    wrapper.unmount()
  })

  it('leaves the keys to whoever is typing', async () => {
    const wrapper = mountView()
    await wrapper.vm.$nextTick()
    const input = document.createElement('input')
    document.body.append(input)
    input.focus()
    press(' ', input)
    await wrapper.vm.$nextTick()
    expect(lastShown()).toEqual(['jan', 'feb', 'mar'])
    input.remove()
    wrapper.unmount()
  })
})
