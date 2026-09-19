import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { defineComponent, h } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { asyncMapComponent } from '@/components/places/asyncMap'

const Loaded = defineComponent({
  name: 'PlacesMap',
  setup: () => () => h('div', { 'data-test': 'loaded-map' }),
})
/** What a dynamic import really hands back: Vue unwraps `default` only from a module. */
const asModule = (component: unknown) => ({ default: component, __esModule: true })

// Inside a parent, as the view has it: an async component swaps its own vnode out, which is only
// observable from the tree around it.
const mountPane = (loader: () => Promise<unknown>, onRetry = vi.fn()) => {
  const Pane = asyncMapComponent(loader as never, onRetry)
  const Host = defineComponent({ setup: () => () => h('div', [h(Pane)]) })
  return { wrapper: mount(Host, { global: { plugins: [PrimeVue] } }), onRetry }
}

describe('asyncMapComponent', () => {
  it('holds the pane with a loading state while the chunk is on its way', () => {
    // The route itself is 12 KB and lands at once; this is what stands in for the megabyte
    // behind it, instead of an empty pane that reads as a broken map.
    const { wrapper } = mountPane(() => new Promise(() => {}))

    expect(wrapper.find('[data-test="map-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="map-load-error"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the map as soon as the chunk arrives', async () => {
    const { wrapper } = mountPane(() => Promise.resolve(asModule(Loaded)))
    await flushPromises()

    expect(wrapper.find('[data-test="loaded-map"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="map-loading"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('offers a way to ask again when the chunk does not arrive', async () => {
    const { wrapper, onRetry } = mountPane(() => Promise.reject(new Error('offline')))
    await flushPromises()

    expect(wrapper.find('[data-test="map-load-error"]').exists()).toBe(true)
    await wrapper.find('[data-test="map-retry"]').trigger('click')

    // Asserted at the decision point: the retry is a request for a NEW async component, because
    // this one has memoised its rejected loader and can only ever fail again.
    expect(onRetry).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
