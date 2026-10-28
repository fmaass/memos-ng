// @vitest-environment jsdom
// jsdom, as TripPassManager.spec.ts: the sheet inside renders the server's QR through DOMPurify.
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import TripPassesPanel from '@/components/TripPassesPanel.vue'
import { aPass, passServer } from '@/composables/__tests__/passServer'

let server: ReturnType<typeof passServer>
vi.mock('@/composables/useApi', () => ({ useApi: () => server.client }))

const trip = (uid: string, title: string) => ({
  uid,
  title,
  kind: 'trip',
  tag: uid,
  spanStart: '2026-10-01',
  spanEnd: '2026-10-10',
  tz: 'Europe/Zurich',
  cover: null,
  memberCount: 3,
  createdBy: 'fabian',
  createdAt: '2026-10-01T00:00:00Z',
  updatedAt: '2026-10-01T00:00:00Z',
})

// PrimeVue's Drawer teleports to the body a tick after mount, so assertions read the document.
async function mountPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const w = mount(TripPassesPanel, {
    attachTo: document.body,
    global: { plugins: [PrimeVue, [VueQueryPlugin, { queryClient }]] },
  })
  await flushPromises()
  return w
}

describe('TripPassesPanel', () => {
  beforeEach(() => {
    server = passServer()
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
    })
  })
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('lists trips only, and opens the pass sheet of the trip that was picked', async () => {
    server.on('GET', '/api/collections?kind=trip', {
      status: 200,
      body: [trip('t1', 'Ticino'), trip('t2', 'Lisbon')],
    })
    server.on('GET', '/api/collections/t2/passes', {
      status: 200,
      body: [aPass({ uid: 'p2', collectionUid: 't2', label: 'Neighbours' })],
    })
    const w = await mountPanel()
    expect(server.calls('GET', '/api/collections?kind=trip')).toHaveLength(1)
    expect(w.findAll('[data-test="trip-row"]').map((r) => r.text())).toEqual([
      expect.stringContaining('Ticino'),
      expect.stringContaining('Lisbon'),
    ])
    expect(document.querySelector('[data-test="trip-pass-manager"]')).toBeNull()

    await w.find('[data-test="trip-row"][data-uid="t2"] [data-test="trip-open-passes"]').trigger('click')
    await flushPromises()

    expect(server.calls('GET', '/api/collections/t2/passes')).toHaveLength(1)
    expect(server.calls('GET', '/api/collections/t1/passes')).toHaveLength(0)
    expect(document.querySelector('[data-test="pass-row"]')?.textContent).toContain('Neighbours')
    expect(document.body.textContent).toContain('Guest passes · Lisbon')
  })

  it('says so when there are no trips', async () => {
    server.on('GET', '/api/collections?kind=trip', { status: 200, body: [] })
    const w = await mountPanel()
    expect(w.find('[data-test="trip-empty"]').exists()).toBe(true)
  })
})
