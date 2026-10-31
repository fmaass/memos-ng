// @vitest-environment jsdom
// jsdom, not happy-dom: happy-dom's parser makes DOMPurify drop the root <svg> of the QR, which a
// real browser (and jsdom, as markdown.spec.ts relies on) keeps.
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import TripPassManager from '@/components/TripPassManager.vue'
import {
  aDevice,
  aPass,
  passServer,
  SERVER_QR_SVG,
} from '@/composables/__tests__/passServer'

let server: ReturnType<typeof passServer>
vi.mock('@/composables/useApi', () => ({ useApi: () => server.client }))

const LIST = '/api/collections/trip1/passes'

function mountSheet(collectionUid = 'trip1') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(TripPassManager, {
    props: { collectionUid },
    global: { plugins: [PrimeVue, [VueQueryPlugin, { queryClient }]] },
  })
}

describe('TripPassManager', () => {
  beforeEach(() => {
    server = passServer()
    // jsdom has no matchMedia; PrimeVue's DatePicker asks it for its responsive breakpoints.
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
    })
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    })
  })
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('issues a pass with the form values and shows its link and QR once', async () => {
    const passes = [aPass({ uid: 'old', label: 'Earlier' })]
    server.on('GET', LIST, () => ({ status: 200, body: passes }))
    server.on('POST', LIST, (req) => {
      const created = aPass({ uid: 'new', label: (req.body as { label: string }).label })
      passes.push(created)
      return {
        status: 201,
        body: { pass: created, secret: 'S3cr3t', url: 'https://door.example/p/S3cr3t', qrSvg: SERVER_QR_SVG },
      }
    })
    const w = mountSheet()
    await flushPromises()

    await w.find('[data-test="pass-label"]').setValue('  Grandparents  ')
    await w.find('[data-test="pass-issue"]').trigger('click')
    await flushPromises()

    const posts = server.calls('POST', LIST)
    expect(posts).toHaveLength(1)
    expect(posts[0].body).toEqual({ label: 'Grandparents', scope: 'contribute', maxDevices: 30 })

    expect(w.find('[data-test="pass-issued-url"]').text()).toBe('https://door.example/p/S3cr3t')
    const qr = w.find('[data-test="pass-qr"] svg')
    expect(qr.exists()).toBe(true)
    expect(qr.find('path').attributes('d')).toBe('M4 4.5h7m1 0h1m2 0h1m1 0h3')

    // The list was re-read after the issue and now holds the new pass.
    expect(server.calls('GET', LIST).length).toBeGreaterThanOrEqual(2)
    expect(w.findAll('[data-test="pass-row"]').map((r) => r.attributes('data-uid'))).toEqual([
      'old',
      'new',
    ])

    // The secret is never written to browser storage.
    expect(JSON.stringify({ ...window.localStorage })).not.toContain('S3cr3t')

    await w.find('[data-test="pass-copy"]').trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('https://door.example/p/S3cr3t')

    await w.find('[data-test="pass-issued-done"]').trigger('click')
    expect(w.find('[data-test="pass-issued-url"]').exists()).toBe(false)
  })

  it('a double click on Issue is one request on the wire', async () => {
    server.on('GET', LIST, { status: 200, body: [] })
    let release!: () => void
    server.on(
      'POST',
      LIST,
      () =>
        new Promise((resolve) => {
          release = () =>
            resolve({
              status: 201,
              body: { pass: aPass(), secret: 's', url: 'https://d/p/s', qrSvg: SERVER_QR_SVG },
            })
        }),
    )
    const w = mountSheet()
    await flushPromises()
    await w.find('[data-test="pass-label"]').setValue('Twice')
    await w.find('[data-test="pass-issue"]').trigger('click')
    await w.find('[data-test="pass-issue"]').trigger('click')
    await flushPromises()
    release()
    await flushPromises()
    expect(server.calls('POST', LIST)).toHaveLength(1)
  })

  it('refuses an empty label without a request', async () => {
    server.on('GET', LIST, { status: 200, body: [] })
    const w = mountSheet()
    await flushPromises()
    await w.find('[data-test="pass-label"]').setValue('   ')
    await w.find('[data-test="pass-issue"]').trigger('click')
    await flushPromises()
    expect(server.calls('POST', LIST)).toHaveLength(0)
    expect(w.find('[data-test="pass-form-error"]').text()).toContain('label')
  })

  it('names the trip-without-tag refusal and shows no QR', async () => {
    server.on('GET', LIST, { status: 200, body: [] })
    server.on('POST', LIST, { status: 409, body: { detail: 'trip_has_no_tag' } })
    const w = mountSheet()
    await flushPromises()
    await w.find('[data-test="pass-label"]').setValue('x')
    await w.find('[data-test="pass-issue"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-test="pass-error"]').text()).toContain('no tag')
    expect(w.find('[data-test="pass-qr"]').exists()).toBe(false)
  })

  it('lists a pass’s devices and revokes one device, re-reading both lists', async () => {
    let devices = [aDevice({ uid: 'd1', displayName: 'Oma' }), aDevice({ uid: 'd2', displayName: 'Opa' })]
    server.on('GET', LIST, () => ({ status: 200, body: [aPass({ deviceCount: devices.filter((d) => !d.revoked).length })] }))
    server.on('GET', '/api/passes/pass1/devices', () => ({ status: 200, body: devices }))
    server.on('DELETE', '/api/passes/pass1/devices/d1', () => {
      devices = devices.map((d) => (d.uid === 'd1' ? { ...d, revoked: true, revokedAt: '2026-10-28T10:00:00Z' } : d))
      return { status: 200, body: devices[0] }
    })
    const w = mountSheet()
    await flushPromises()
    await w.find('[data-test="pass-devices-toggle"]').trigger('click')
    await flushPromises()
    expect(w.findAll('[data-test="device-row"]').map((r) => r.text())).toEqual([
      expect.stringContaining('Oma'),
      expect.stringContaining('Opa'),
    ])
    const devicesBefore = server.calls('GET', '/api/passes/pass1/devices').length
    const listBefore = server.calls('GET', LIST).length

    await w.find('[data-test="device-revoke"][data-uid="d1"]').trigger('click')
    await flushPromises()

    expect(server.calls('DELETE', '/api/passes/pass1/devices/d1')).toHaveLength(1)
    expect(server.calls('DELETE', '/api/passes/pass1')).toHaveLength(0)
    expect(server.calls('GET', '/api/passes/pass1/devices').length).toBe(devicesBefore + 1)
    expect(server.calls('GET', LIST).length).toBe(listBefore + 1)
    const oma = w.find('[data-test="device-row"][data-uid="d1"]')
    expect(oma.text()).toContain('Revoked')
    expect(oma.find('[data-test="device-revoke"]').exists()).toBe(false)
    expect(w.find('[data-test="device-row"][data-uid="d2"] [data-test="device-revoke"]').exists()).toBe(true)
  })

  it('revokes one device, then the whole pass after a confirm, and both devices read back revoked', async () => {
    let pass = aPass()
    let devices = [aDevice({ uid: 'd1', displayName: 'Oma' }), aDevice({ uid: 'd2', displayName: 'Opa' })]
    const revokedAt = '2026-10-28T10:00:00Z'
    server.on('GET', LIST, () => ({ status: 200, body: [pass] }))
    server.on('GET', '/api/passes/pass1/devices', () => ({ status: 200, body: devices }))
    server.on('DELETE', '/api/passes/pass1/devices/d1', () => {
      devices = devices.map((d) => (d.uid === 'd1' ? { ...d, revoked: true, revokedAt } : d))
      return { status: 200, body: devices[0] }
    })
    server.on('DELETE', '/api/passes/pass1', () => {
      pass = { ...pass, state: 'revoked', revokedAt, version: 3, revokedVersion: 3 }
      // The server cascades the revoke to every device still live on the pass.
      devices = devices.map((d) => (d.revoked ? d : { ...d, revoked: true, revokedAt }))
      return { status: 200, body: pass }
    })
    const w = mountSheet()
    await flushPromises()
    await w.find('[data-test="pass-devices-toggle"]').trigger('click')
    await flushPromises()

    await w.find('[data-test="device-revoke"][data-uid="d1"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-test="device-row"][data-uid="d1"]').text()).toContain('Revoked')
    expect(w.find('[data-test="device-row"][data-uid="d2"]').text()).not.toContain('Revoked')

    await w.find('[data-test="pass-revoke"]').trigger('click')
    await flushPromises()
    // First click only arms the confirm; nothing is on the wire yet.
    expect(server.calls('DELETE', '/api/passes/pass1')).toHaveLength(0)
    await w.find('[data-test="pass-revoke-confirm"]').trigger('click')
    await flushPromises()
    expect(server.calls('DELETE', '/api/passes/pass1')).toHaveLength(1)
    const row = w.find('[data-test="pass-row"][data-uid="pass1"]')
    expect(row.find('[data-test="pass-state"]').text()).toBe('revoked')
    expect(row.find('[data-test="pass-revoke"]').exists()).toBe(false)
    // The open device list is re-read, so neither device still offers a revoke of a dead pass.
    for (const uid of ['d1', 'd2']) {
      const device = w.find(`[data-test="device-row"][data-uid="${uid}"]`)
      expect(device.text()).toContain('Revoked')
      expect(device.find('[data-test="device-revoke"]').exists()).toBe(false)
    }
  })

  it('a pass revoked elsewhere clears the just-issued QR when this sheet’s revoke reads 410', async () => {
    const passes = [aPass({ uid: 'new', label: 'Guests' })]
    server.on('GET', LIST, () => ({ status: 200, body: passes }))
    server.on('POST', LIST, () => ({
      status: 201,
      body: { pass: passes[0], secret: 'S3cr3t', url: 'https://door.example/p/S3cr3t', qrSvg: SERVER_QR_SVG },
    }))
    server.on('DELETE', '/api/passes/new', { status: 410, body: { detail: 'pass_revoked' } })
    const w = mountSheet()
    await flushPromises()
    await w.find('[data-test="pass-label"]').setValue('Guests')
    await w.find('[data-test="pass-issue"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-test="pass-qr"] svg').exists()).toBe(true)

    await w.find('[data-test="pass-row"][data-uid="new"] [data-test="pass-revoke"]').trigger('click')
    await w.find('[data-test="pass-revoke-confirm"]').trigger('click')
    await flushPromises()
    expect(server.calls('DELETE', '/api/passes/new')).toHaveLength(1)
    expect(w.find('[data-test="pass-error"]').text()).toContain('already revoked')
    expect(w.find('[data-test="pass-qr"]').exists()).toBe(false)
    expect(w.find('[data-test="pass-issued-url"]').exists()).toBe(false)
  })

  it('a revoke refused for another reason keeps the just-issued QR', async () => {
    const passes = [aPass({ uid: 'new', label: 'Guests' })]
    server.on('GET', LIST, () => ({ status: 200, body: passes }))
    server.on('POST', LIST, () => ({
      status: 201,
      body: { pass: passes[0], secret: 'S3cr3t', url: 'https://door.example/p/S3cr3t', qrSvg: SERVER_QR_SVG },
    }))
    server.on('DELETE', '/api/passes/new', { status: 500, body: { detail: 'boom' } })
    const w = mountSheet()
    await flushPromises()
    await w.find('[data-test="pass-label"]').setValue('Guests')
    await w.find('[data-test="pass-issue"]').trigger('click')
    await flushPromises()
    await w.find('[data-test="pass-row"][data-uid="new"] [data-test="pass-revoke"]').trigger('click')
    await w.find('[data-test="pass-revoke-confirm"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-test="pass-error"]').exists()).toBe(true)
    expect(w.find('[data-test="pass-qr"] svg').exists()).toBe(true)
  })

  it('a revoke the server refuses as already revoked says so and re-reads the list', async () => {
    server.on('GET', LIST, { status: 200, body: [aPass()] })
    server.on('DELETE', '/api/passes/pass1', { status: 410, body: { detail: 'pass_revoked' } })
    const w = mountSheet()
    await flushPromises()
    const before = server.calls('GET', LIST).length
    await w.find('[data-test="pass-revoke"]').trigger('click')
    await w.find('[data-test="pass-revoke-confirm"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-test="pass-error"]').text()).toContain('already revoked')
    expect(server.calls('GET', LIST).length).toBe(before + 1)
  })

  it('renders a pass label that looks like markup as text, never as markup', async () => {
    server.on('GET', LIST, { status: 200, body: [aPass({ label: '<img src=x onerror="alert(1)">Oma' })] })
    const w = mountSheet()
    await flushPromises()
    const row = w.find('[data-test="pass-row"]')
    expect(row.find('img').exists()).toBe(false)
    expect(row.text()).toContain('<img src=x onerror="alert(1)">Oma')
  })

  it('offers only the scope a guest route serves today, not view access', async () => {
    server.on('GET', LIST, { status: 200, body: [] })
    const w = mountSheet()
    await flushPromises()
    const options = w.findAll('[data-test="pass-scope"] [role="radio"], [data-test="pass-scope"] button')
    expect(options.map((o) => o.text())).toEqual(['Add pictures'])
    expect(w.find('[data-test="pass-scope"]').text()).not.toContain('view')
  })

  it('shows the empty state when the trip has no passes', async () => {
    server.on('GET', LIST, { status: 200, body: [] })
    const w = mountSheet()
    await flushPromises()
    expect(w.find('[data-test="pass-empty"]').exists()).toBe(true)
  })
})
