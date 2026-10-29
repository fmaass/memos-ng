import { flushPromises, mount } from '@vue/test-utils'
import { IDBFactory } from 'fake-indexeddb'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import GuestApp from '../GuestApp.vue'
import { openBindings } from '../bindings'
import { CREDENTIAL_KEY } from '../credential'
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
