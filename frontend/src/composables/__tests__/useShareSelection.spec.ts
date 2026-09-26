import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { buildShareFiles, extensionFor, useShareSelection } from '@/composables/useShareSelection'

const { toastAdd } = vi.hoisted(() => ({ toastAdd: vi.fn() }))
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))

const item = (uid: string, filename: string) => ({
  uid,
  filename,
  downloadUrl: `/api/attachments/${uid}/original`,
})
const bytes = (type: string, status = 200) =>
  new Response(new Blob(['x'], { type }), { status, headers: { 'Content-Type': type } })

function stubNavigator(canShare: ((data: ShareData) => boolean) | undefined, share = vi.fn(async () => {})) {
  Object.defineProperty(navigator, 'canShare', { value: canShare, configurable: true, writable: true })
  Object.defineProperty(navigator, 'share', { value: share, configurable: true, writable: true })
  return share
}

function mountHost() {
  let api!: ReturnType<typeof useShareSelection>
  mount(defineComponent({ setup: () => ((api = useShareSelection()), () => null) }))
  return api
}

describe('useShareSelection', () => {
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>()
  beforeEach(() => {
    fetchMock.mockReset()
    toastAdd.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    Reflect.deleteProperty(navigator, 'canShare')
    Reflect.deleteProperty(navigator, 'share')
  })

  it('fetches every ORIGINAL with the session cookie and an Accept that forbids a negotiated WebP', async () => {
    fetchMock.mockImplementation(async () => bytes('image/jpeg'))
    await buildShareFiles([item('a', 'a.jpg'), item('b', 'b.jpg')])
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/attachments/a/original',
      '/api/attachments/b/original',
    ])
    for (const [, init] of fetchMock.mock.calls) {
      expect(init).toMatchObject({ credentials: 'include', headers: { Accept: '*/*' } })
    }
  })

  it('names a file after the item, and only an item without a filename after its uid and the response type', async () => {
    fetchMock
      .mockResolvedValueOnce(bytes('image/jpeg'))
      .mockResolvedValueOnce(bytes('video/quicktime'))
    const files = await buildShareFiles([item('a', 'IMG_0001.HEIC'), item('b', '')])
    expect(files!.map((f) => f.name)).toEqual(['IMG_0001.HEIC', 'b.mov'])
    expect(files!.map((f) => f.type)).toEqual(['image/jpeg', 'video/quicktime'])
  })

  it('a non-OK response aborts before any file is built', async () => {
    fetchMock.mockResolvedValueOnce(bytes('image/jpeg')).mockResolvedValueOnce(bytes('text/plain', 404))
    let built = 0
    const RealFile = File
    vi.stubGlobal(
      'File',
      class extends RealFile {
        constructor(...args: ConstructorParameters<typeof RealFile>) {
          super(...args)
          built++
        }
      },
    )
    expect(await buildShareFiles([item('a', 'a.jpg'), item('b', 'b.jpg')])).toBeNull()
    expect(built).toBe(0)
  })

  it('a transport failure aborts too', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    expect(await buildShareFiles([item('a', 'a.jpg')])).toBeNull()
  })

  it.each([
    ['image/jpeg', 'jpg'],
    ['image/png', 'png'],
    ['image/heic', 'heic'],
    ['video/mp4', 'mp4'],
    ['video/quicktime', 'mov'],
    ['image/jpeg; charset=binary', 'jpg'],
    ['image/x-canon-cr2', 'x-canon-cr2'],
    ['', 'bin'],
  ])('%s → .%s', (type, ext) => {
    expect(extensionFor(type)).toBe(ext)
  })

  it('hands exactly one File per selected item to navigator.share', async () => {
    const share = stubNavigator(() => true)
    fetchMock.mockImplementation(async () => bytes('image/jpeg'))
    const api = mountHost()
    await api.share([item('a', 'a.jpg'), item('b', 'b.jpg')])
    expect(share).toHaveBeenCalledTimes(1)
    const { files } = (share.mock.calls[0] as unknown as [ShareData])[0]
    expect(files!.map((f) => f.name)).toEqual(['a.jpg', 'b.jpg'])
    expect(files!.every((f) => f instanceof File)).toBe(true)
  })

  it('does not call navigator.share when a fetch failed, and says so', async () => {
    const share = stubNavigator(() => true)
    fetchMock.mockResolvedValueOnce(bytes('image/jpeg', 500))
    const api = mountHost()
    await api.share([item('a', 'a.jpg')])
    expect(share).not.toHaveBeenCalled()
    expect(toastAdd).toHaveBeenCalledTimes(1)
  })

  it('a cancelled share sheet is not an error', async () => {
    stubNavigator(() => true, vi.fn(async () => Promise.reject(new DOMException('cancel', 'AbortError'))))
    fetchMock.mockImplementation(async () => bytes('image/jpeg'))
    const api = mountHost()
    await api.share([item('a', 'a.jpg')])
    expect(toastAdd).not.toHaveBeenCalled()
  })

  it('is pending while it fetches and refuses a second share meanwhile', async () => {
    const share = stubNavigator(() => true)
    let release!: () => void
    fetchMock.mockImplementationOnce(() => new Promise((r) => (release = () => r(bytes('image/jpeg')))))
    const api = mountHost()
    const first = api.share([item('a', 'a.jpg')])
    expect(api.pending.value).toBe(true)
    await api.share([item('a', 'a.jpg')])
    release()
    await first
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(share).toHaveBeenCalledTimes(1)
    expect(api.pending.value).toBe(false)
  })

  it.each([
    ['canShare accepts files', () => true, true],
    ['canShare refuses files', () => false, false],
    ['no canShare at all', undefined, false],
  ])('supported when %s', (_, canShare, expected) => {
    stubNavigator(canShare)
    expect(mountHost().supported).toBe(expected)
  })

  it('probes canShare with a File', () => {
    const canShare = vi.fn(() => true)
    stubNavigator(canShare)
    mountHost()
    const [data] = canShare.mock.calls[0] as unknown as [ShareData]
    expect(data.files).toHaveLength(1)
    expect(data.files![0]).toBeInstanceOf(File)
  })
})
