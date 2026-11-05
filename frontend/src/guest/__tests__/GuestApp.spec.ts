import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { IDBFactory } from 'fake-indexeddb'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import GuestApp from '../GuestApp.vue'
import { openBindings } from '../bindings'
import { CREDENTIAL_KEY } from '../credential'
import { digestFile } from '../digest'
import { browserNetwork } from '../uploader'
import { FakeDoor, bytes, fileOf, sha } from './fakeDoor'

// The IndexedDB transport, held shut until released: the window in which the page knows the pass
// but has no bindings and so no uploader yet. Handlers set on the stand-in request are forwarded
// to the real (fake-indexeddb) request once it exists.
function gatedIndexedDB() {
  const real = new IDBFactory()
  let release!: () => void
  const opened = new Promise<void>((resolve) => (release = resolve))
  let opens = 0
  const factory = {
    open(name: string, version?: number) {
      opens++
      const stub = {
        onupgradeneeded: null as ((e: Event) => void) | null,
        onsuccess: null as ((e: Event) => void) | null,
        onerror: null as ((e: Event) => void) | null,
        inner: null as IDBOpenDBRequest | null,
        get result() {
          return stub.inner!.result
        },
        get error() {
          return stub.inner!.error
        },
      }
      void opened.then(() => {
        const inner = real.open(name, version)
        stub.inner = inner
        inner.onupgradeneeded = (e) => stub.onupgradeneeded?.(e)
        inner.onsuccess = (e) => stub.onsuccess?.(e)
        inner.onerror = (e) => stub.onerror?.(e)
      })
      return stub
    },
  }
  return { factory, release, real, opens: () => opens }
}

class SilentWorker {
  onmessage: ((e: MessageEvent) => void) | null = null
  postMessage(): void {}
  terminate(): void {}
}

describe('GuestApp picker', () => {
  let door: FakeDoor

  beforeEach(() => {
    door = new FakeDoor()
    localStorage.setItem(CREDENTIAL_KEY, JSON.stringify({ token: 'trip_dev_one', deviceId: 'dev1' }))
    history.replaceState(null, '', '/p/joined')
    vi.stubGlobal('fetch', (input: string, init: RequestInit) => door.fetch(input, init))
    vi.stubGlobal('Worker', SilentWorker)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  it('offers no picker while the uploader is not ready, so no selection is dropped', async () => {
    const idb = gatedIndexedDB()
    vi.stubGlobal('indexedDB', idb.factory)
    const wrapper = mount(GuestApp)
    await vi.waitFor(() => expect(wrapper.text()).toContain('sending as Anna'))
    await flushPromises()

    // The pass is ready but the bindings are still opening: a picker here would hand its files to
    // an uploader that does not exist yet, and the selection would vanish.
    expect(wrapper.find('input[type=file]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Preparing')

    idb.release()
    await vi.waitFor(() => expect(wrapper.find('input[type=file]').exists()).toBe(true))
    const input = wrapper.find('input[type=file]')
    Object.defineProperty(input.element, 'files', { value: [fileOf(bytes(64, 1), 'IMG_0001.jpg')], configurable: true })
    await input.trigger('change')
    expect(wrapper.find('.files .name').text()).toBe('IMG_0001.jpg')
    wrapper.unmount()
  })

  it('a second Join starts no second uploader: one bindings open, one completion replay', async () => {
    localStorage.clear()
    history.replaceState(null, '', '/p/s3cret')
    const idb = gatedIndexedDB()
    // A completion an earlier tab of this device persisted; its upload is fully received.
    const data = bytes(64, 7)
    door.uploads.set('u900', { uid: 'u900', filename: 'a.jpg', size: 64, sha256: sha(data), data })
    const seeded = await openBindings(idb.real)
    await seeded.putPending({ deviceId: 'dev1', uploads: [{ uid: 'u900', sha256: sha(data), size: 64 }], caption: null })
    vi.stubGlobal('indexedDB', idb.factory)
    const wrapper = mount(GuestApp)
    await vi.waitFor(() => expect(wrapper.find('#guest-name').exists()).toBe(true))
    await wrapper.find('#guest-name').setValue('Anna')
    const form = wrapper.find('form')
    void form.trigger('submit')
    void form.trigger('submit') // a double tap, before the first redeem has answered
    await vi.waitFor(() => expect(wrapper.text()).toContain('sending as Anna'))
    await flushPromises()
    idb.release()
    await vi.waitFor(() => expect(door.calls('POST', '/api/guest/memos').length).toBeGreaterThan(0))
    for (let i = 0; i < 20; i++) await flushPromises()
    expect(door.calls('POST', '/api/pass/redeem')).toHaveLength(1)
    expect(idb.opens()).toBe(1)
    expect(door.calls('POST', '/api/guest/memos')).toHaveLength(1)
    wrapper.unmount()
  })
})

class HashingWorker {
  onmessage: ((e: MessageEvent) => void) | null = null
  postMessage({ id, file }: { id: number; file: Blob }): void {
    void digestFile(file, () => {}).then(({ sha256, head }) =>
      this.onmessage?.({ data: { id, type: 'done', sha256, head } } as MessageEvent),
    )
  }
  terminate(): void {}
}

describe('GuestApp Retry all', () => {
  let door: FakeDoor

  const pick = async (wrapper: VueWrapper, files: File[]) => {
    await vi.waitFor(() => expect(wrapper.find('input[type=file]').exists()).toBe(true))
    const input = wrapper.find('input[type=file]')
    Object.defineProperty(input.element, 'files', { value: files, configurable: true })
    await input.trigger('change')
  }
  const retryAll = (wrapper: VueWrapper) => wrapper.findAll('button').filter((b) => b.text() === 'Retry all')
  const states = (wrapper: VueWrapper) => wrapper.findAll('.files li').map((li) => li.classes()[0])

  beforeEach(() => {
    door = new FakeDoor()
    localStorage.setItem(CREDENTIAL_KEY, JSON.stringify({ token: 'trip_dev_one', deviceId: 'dev1' }))
    history.replaceState(null, '', '/p/joined')
    vi.stubGlobal('fetch', (input: string, init: RequestInit) => door.fetch(input, init))
    vi.stubGlobal('Worker', HashingWorker)
    vi.stubGlobal('indexedDB', new IDBFactory())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  it('shows one button only while an item is Failed, and sends every Failed item again', async () => {
    door.createFaults = [{ status: 400, detail: 'not now' }, { status: 400, detail: 'not now' }]
    const wrapper = mount(GuestApp)
    await pick(wrapper, [fileOf(bytes(64, 21), 'a.jpg'), fileOf(bytes(64, 22), 'b.jpg')])
    await vi.waitFor(() => expect(states(wrapper)).toEqual(['failed', 'failed']))
    expect(retryAll(wrapper)).toHaveLength(1)

    await retryAll(wrapper)[0].trigger('click')
    await vi.waitFor(() => expect(states(wrapper)).toEqual(['received', 'received']))
    expect(retryAll(wrapper)).toHaveLength(0)
    expect(door.calls('POST', '/api/guest/uploads').filter((w) => w.status === 201)).toHaveLength(2)
    wrapper.unmount()
  })

  it('leaves a file alone that did not fail, and a name that merely says Retry all shows nothing', async () => {
    const wrapper = mount(GuestApp)
    await pick(wrapper, [fileOf(bytes(64, 23), 'Retry all.jpg')])
    await vi.waitFor(() => expect(states(wrapper)).toEqual(['received']))
    expect(wrapper.text()).toContain('Retry all.jpg')
    expect(retryAll(wrapper)).toHaveLength(0)
    wrapper.unmount()
  })

  it.each<[number, string]>([
    [415, 'unsupported'],
    [413, 'too-large'],
    [507, 'quota'],
  ])('a %i is a verdict, not a failure: state %s offers no Retry all', async (status, state) => {
    door.createFaults = [{ status, detail: 'no' }]
    const wrapper = mount(GuestApp)
    await pick(wrapper, [fileOf(bytes(64, 24), 'a.jpg')])
    await vi.waitFor(() => expect(states(wrapper)).toEqual([state]))
    expect(retryAll(wrapper)).toHaveLength(0)
    wrapper.unmount()
  })

  it('goes away on the click even while the retried create is still out, and a double tap sends once', async () => {
    door.createFaults = [{ status: 400, detail: 'not now' }]
    const wrapper = mount(GuestApp)
    await pick(wrapper, [fileOf(bytes(64, 25), 'a.jpg')])
    await vi.waitFor(() => expect(states(wrapper)).toEqual(['failed']))
    const orig = door.fetch
    let release!: () => void
    const held = new Promise<void>((r) => (release = r))
    door.fetch = async (input, init) => {
      if (input === '/api/guest/uploads') await held
      return orig(input, init)
    }
    const button = retryAll(wrapper)[0]
    void button.trigger('click')
    await button.trigger('click')
    await flushPromises()
    expect(retryAll(wrapper)).toHaveLength(0)
    expect(states(wrapper)).not.toContain('failed')
    release()
    await vi.waitFor(() => expect(states(wrapper)).toEqual(['received']))
    expect(door.calls('POST', '/api/guest/uploads').filter((w) => w.status === 201)).toHaveLength(1)
    wrapper.unmount()
  })
})

describe('the browser network', () => {
  afterEach(() => {
    Reflect.deleteProperty(navigator, 'onLine')
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  const setOnLine = (value: boolean) => Object.defineProperty(navigator, 'onLine', { value, configurable: true })

  it('reads navigator.onLine', () => {
    setOnLine(false)
    expect(browserNetwork.online()).toBe(false)
    setOnLine(true)
    expect(browserNetwork.online()).toBe(true)
  })

  it('hears the online event until it is unsubscribed, and leaves no listener behind', () => {
    const listener = vi.fn()
    const off = browserNetwork.onOnline(listener)
    window.dispatchEvent(new Event('online'))
    expect(listener).toHaveBeenCalledTimes(1)
    off()
    window.dispatchEvent(new Event('online'))
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('is what the page runs on: a real online event sends the waiting create at once', async () => {
    const door = new FakeDoor()
    localStorage.setItem(CREDENTIAL_KEY, JSON.stringify({ token: 'trip_dev_one', deviceId: 'dev1' }))
    history.replaceState(null, '', '/p/joined')
    vi.stubGlobal('fetch', (input: string, init: RequestInit) => door.fetch(input, init))
    vi.stubGlobal('Worker', HashingWorker)
    vi.stubGlobal('indexedDB', new IDBFactory())
    const wrapper = mount(GuestApp)
    await vi.waitFor(() => expect(wrapper.find('input[type=file]').exists()).toBe(true))
    door.outage = 'network' // the page itself loaded before the drop
    setOnLine(false)
    const input = wrapper.find('input[type=file]')
    Object.defineProperty(input.element, 'files', { value: [fileOf(bytes(64, 26), 'a.jpg')], configurable: true })
    await input.trigger('change')
    await vi.waitFor(() => expect(door.calls('POST', '/api/guest/uploads')).toHaveLength(1))
    await flushPromises()
    door.outage = null
    setOnLine(true)
    window.dispatchEvent(new Event('online'))
    await vi.waitFor(() => expect(wrapper.find('.files li').classes()).toContain('received'))
    wrapper.unmount()
  })
})
