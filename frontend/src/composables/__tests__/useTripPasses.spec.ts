import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref } from 'vue'

import { usePassDevices, useTripPasses } from '@/composables/useTripPasses'

import { aDevice, aPass, passServer, SERVER_QR_SVG } from './passServer'

let server: ReturnType<typeof passServer>
vi.mock('@/composables/useApi', () => ({ useApi: () => server.client }))

/** Run the composable inside a real component with a real query client. */
function withPasses(initialUid = 'trip1') {
  const uid = ref(initialUid)
  let api!: ReturnType<typeof useTripPasses>
  const Host = defineComponent({
    setup() {
      api = useTripPasses(uid)
      return () => h('div')
    },
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  mount(Host, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } })
  return { uid, api: () => api }
}

function held<T>() {
  let release!: (v: T) => void
  const promise = new Promise<T>((r) => (release = r))
  return { promise, release }
}

const issued = (uid = 'pass1') => ({
  status: 201,
  body: { pass: aPass({ uid }), secret: 'S', url: 'https://d/p/S', qrSvg: SERVER_QR_SVG },
})

describe('useTripPasses', () => {
  beforeEach(() => {
    server = passServer()
    server.on('GET', '/api/collections/trip1/passes', { status: 200, body: [aPass()] })
    server.on('GET', '/api/collections/trip2/passes', { status: 200, body: [] })
  })

  it('reads the trip’s passes from its own collection route', async () => {
    const { api } = withPasses()
    await flushPromises()
    expect(server.calls('GET', '/api/collections/trip1/passes')).toHaveLength(1)
    expect(api().passes.data.value?.map((p) => p.uid)).toEqual(['pass1'])
  })

  it('joins a second issue while the first is on the wire: one POST, one answer', async () => {
    const gate = held<{ status: number; body: unknown }>()
    server.on('POST', '/api/collections/trip1/passes', () => gate.promise)
    const { api } = withPasses()
    await flushPromises()
    const first = api().issue({ label: 'a', scope: 'contribute', maxDevices: 30 })
    const second = api().issue({ label: 'a', scope: 'contribute', maxDevices: 30 })
    expect(api().issuing.value).toBe(true)
    gate.release(issued())
    const [a, b] = await Promise.all([first, second])
    expect(server.calls('POST', '/api/collections/trip1/passes')).toHaveLength(1)
    expect(a).toBe(b)
    expect(api().issuing.value).toBe(false)
    // Once settled, a new issue is a new request.
    server.on('POST', '/api/collections/trip1/passes', issued('pass2'))
    await api().issue({ label: 'b', scope: 'contribute', maxDevices: 30 })
    expect(server.calls('POST', '/api/collections/trip1/passes')).toHaveLength(2)
  })

  it('an issue answer that lands after the sheet moved to another trip is not shown there', async () => {
    const gate = held<{ status: number; body: unknown }>()
    server.on('POST', '/api/collections/trip1/passes', () => gate.promise)
    const { uid, api } = withPasses()
    await flushPromises()
    const pending = api().issue({ label: 'a', scope: 'contribute', maxDevices: 30 })
    uid.value = 'trip2'
    gate.release(issued())
    await pending
    await flushPromises()
    expect(api().issuedHere.value).toBeNull()
    uid.value = 'trip1'
    expect(api().issuedHere.value).toBeNull()
  })

  it('a refusal from the old trip does not surface as an error on the new one', async () => {
    const gate = held<{ status: number; body: unknown }>()
    server.on('POST', '/api/collections/trip1/passes', () => gate.promise)
    const { uid, api } = withPasses()
    await flushPromises()
    const pending = api().issue({ label: 'a', scope: 'contribute', maxDevices: 30 })
    uid.value = 'trip2'
    gate.release({ status: 409, body: { detail: 'trip_has_no_tag' } })
    expect(await pending).toBeNull()
    expect(api().error.value).toBeNull()
  })

  it('revoking the pass whose QR is on screen takes the QR away', async () => {
    server.on('POST', '/api/collections/trip1/passes', issued('pass1'))
    server.on('DELETE', '/api/passes/pass1', {
      status: 200,
      body: aPass({ state: 'revoked', revokedAt: '2026-10-28T10:00:00Z' }),
    })
    const { api } = withPasses()
    await flushPromises()
    await api().issue({ label: 'a', scope: 'contribute', maxDevices: 30 })
    expect(api().issuedHere.value?.url).toBe('https://d/p/S')
    expect(await api().revokePass('pass1')).toBe(true)
    expect(api().issuedHere.value).toBeNull()
  })

  it('joins a second revoke of the same device and keeps revokes of different devices apart', async () => {
    const gate = held<{ status: number; body: unknown }>()
    server.on('DELETE', '/api/passes/pass1/devices/d1', () => gate.promise)
    server.on('DELETE', '/api/passes/pass1/devices/d2', {
      status: 200,
      body: aDevice({ uid: 'd2', revoked: true }),
    })
    const { api } = withPasses()
    await flushPromises()
    const a = api().revokeDevice('pass1', 'd1')
    const b = api().revokeDevice('pass1', 'd1')
    expect(api().revokingDevice('pass1', 'd1')).toBe(true)
    expect(api().revokingDevice('pass1', 'd2')).toBe(false)
    await api().revokeDevice('pass1', 'd2')
    gate.release({ status: 200, body: aDevice({ uid: 'd1', revoked: true }) })
    expect(await Promise.all([a, b])).toEqual([true, true])
    expect(server.calls('DELETE', '/api/passes/pass1/devices/d1')).toHaveLength(1)
    expect(server.calls('DELETE', '/api/passes/pass1/devices/d2')).toHaveLength(1)
  })

  it('names an already-revoked device and still re-reads the pass list', async () => {
    server.on('DELETE', '/api/passes/pass1/devices/d1', {
      status: 410,
      body: { detail: 'device_revoked' },
    })
    const { api } = withPasses()
    await flushPromises()
    const before = server.calls('GET', '/api/collections/trip1/passes').length
    expect(await api().revokeDevice('pass1', 'd1')).toBe(false)
    await flushPromises()
    expect(api().error.value).toBe('That device is already revoked.')
    expect(server.calls('GET', '/api/collections/trip1/passes').length).toBe(before + 1)
  })
})

describe('usePassDevices', () => {
  beforeEach(() => {
    server = passServer()
  })

  it('reads one pass’s devices from its devices route', async () => {
    server.on('GET', '/api/passes/pass9/devices', { status: 200, body: [aDevice({ uid: 'dz' })] })
    let q!: ReturnType<typeof usePassDevices>
    const Host = defineComponent({
      setup() {
        q = usePassDevices('pass9')
        return () => h('div')
      },
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mount(Host, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } })
    await flushPromises()
    expect(q.data.value?.map((d) => d.uid)).toEqual(['dz'])
  })

  it('surfaces a 404 as a PassError with the server’s status', async () => {
    server.on('GET', '/api/passes/gone/devices', { status: 404, body: { detail: 'Pass not found' } })
    let q!: ReturnType<typeof usePassDevices>
    const Host = defineComponent({
      setup() {
        q = usePassDevices('gone')
        return () => h('div')
      },
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mount(Host, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } })
    await flushPromises()
    expect(q.isError.value).toBe(true)
    expect((q.error.value as { status?: number }).status).toBe(404)
  })
})
