import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ClusterDrawer from '@/components/places/ClusterDrawer.vue'
import { marker } from '@/lib/places/__tests__/fixtures'

// The REAL drawer around the REAL gallery: PlacesView.spec stubs the drawer with a count-only
// stand-in, so this is the one place that proves a tapped cluster photo opens the photo it shows.
// PrimeVue's Drawer teleports to the body a tick after mounting, so the assertions read the document.
const markers = [
  marker({ uid: 'm0' }),
  marker({ uid: 'm1', mime: 'video/mp4', width: 1920, height: 1080 }),
  marker({ uid: 'm2', width: 3000, height: 4000 }),
]

let wrapper: VueWrapper | null = null

async function mountDrawer() {
  wrapper = mount(ClusterDrawer, {
    attachTo: document.body,
    global: { plugins: [PrimeVue] },
    props: { visible: true, markers, clusterId: 7 },
  })
  await flushPromises()
  return wrapper
}

function tiles(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[data-test="gallery-img"]'))
}

describe('ClusterDrawer', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(private cb: ResizeObserverCallback) {}
        observe() {
          this.cb([{ contentRect: { width: 320 } } as ResizeObserverEntry], this as ResizeObserver)
        }
        disconnect() {}
        unobserve() {}
      },
    )
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
  })

  it('renders one tile per marker in the cluster', async () => {
    await mountDrawer()
    expect(document.querySelector('[data-test="cluster-count"]')?.textContent).toContain(
      '3 photos here',
    )
    expect(tiles()).toHaveLength(markers.length)
  })

  it("opens the tapped tile's marker by its index in the cluster", async () => {
    const w = await mountDrawer()
    tiles()[2]!.click()
    tiles()[0]!.click()
    tiles()[1]!.click()
    expect(w.emitted('open')).toEqual([[2], [0], [1]])
  })
})
