import { QueryClient, type QueryKey, VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  attachPrintMaster,
  fetchPrintMasters,
  printMasterUploads,
  removePrintMaster,
  usePrintMaster,
} from '@/composables/usePrintMaster'

// PrimeVue's toast is a dependency of the unit, not the unit itself — mock it so the emitted
// messages are inspectable without mounting the <Toast /> outlet.
const { toastAdd } = vi.hoisted(() => ({ toastAdd: vi.fn() }))
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))

const fetchMock = vi.fn()

function json(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    headers: new Headers(),
  } as unknown as Response
}

function master(): File {
  return new File(['tiff-bytes'], 'master.tif', { type: 'image/tiff' })
}

/** Route the resumable-upload handshake, and let the caller decide what the bind call answers. */
function routeUpload(bind: Response = json(null, 201)) {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    if (url === '/api/uploads') {
      return Promise.resolve(json({ upload_uid: 'u1', chunk_size: 1 << 20, bytes_received: 0 }))
    }
    if (url.startsWith('/api/uploads/') && init?.method === 'PATCH') {
      return Promise.resolve(json({ bytes_received: 10 }))
    }
    return Promise.resolve(bind)
  })
}

function mountPrintMaster(invalidateKey: QueryKey = ['memos']) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue()
  let composable!: ReturnType<typeof usePrintMaster>
  const Host = defineComponent({
    setup() {
      composable = usePrintMaster(invalidateKey)
      return () => h('div')
    },
  })
  const wrapper = mount(Host, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } })
  return { composable, invalidate, wrapper }
}

describe('attachPrintMaster', () => {
  beforeEach(() => vi.stubGlobal('fetch', fetchMock))
  afterEach(() => {
    vi.unstubAllGlobals()
    fetchMock.mockReset()
  })

  it('pushes the file through the RESUMABLE upload, then binds it to the photo', async () => {
    // The old path POSTed one multipart blob to /print, which wrote the legacy print_path column —
    // NULL for every master in production. The bytes now travel the chunked route and land in
    // attachment_renditions.
    routeUpload()

    await attachPrintMaster({ uid: 'a1', file: master(), variantKey: 'print' })

    const urls = fetchMock.mock.calls.map((c) => c[0])
    expect(urls[0]).toBe('/api/uploads')
    expect(urls[1]).toBe('/api/uploads/u1')
    expect(urls[2]).toBe('/api/attachments/a1/renditions')
    expect(urls).not.toContain('/api/attachments/a1/print') // never the legacy column again
    const bind = fetchMock.mock.calls[2][1]
    expect(bind.method).toBe('POST')
    expect(JSON.parse(bind.body)).toEqual({ upload_uid: 'u1', variant_key: 'print' })
  })

  it('carries the master’s own key so a replace does not mint a second, competing master', async () => {
    routeUpload()
    await attachPrintMaster({ uid: 'a1', file: master(), variantKey: 'print:glossy' })
    expect(JSON.parse(fetchMock.mock.calls[2][1].body).variant_key).toBe('print:glossy')
  })

  it('reports progress from the upload it is driving', async () => {
    routeUpload()
    const seen: number[] = []
    await attachPrintMaster({ uid: 'a1', file: master(), variantKey: 'print' }, (pct) =>
      seen.push(pct),
    )
    expect(seen).toContain(100)
  })

  it.each([
    ['4xx', 413],
    ['5xx', 500],
  ])('rejects with the status when the server refuses the bind (%s)', async (_l, status) => {
    routeUpload(json(null, status))
    await expect(
      attachPrintMaster({ uid: 'a1', file: master(), variantKey: 'print' }),
    ).rejects.toThrow(String(status))
  })
})

describe('removePrintMaster / fetchPrintMasters', () => {
  beforeEach(() => vi.stubGlobal('fetch', fetchMock))
  afterEach(() => {
    vi.unstubAllGlobals()
    fetchMock.mockReset()
  })

  it('DELETEs the master by its variant key, url-encoded', async () => {
    fetchMock.mockResolvedValue(json(null, 204))
    await removePrintMaster({ uid: 'a1', variantKey: 'print:glossy' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/attachments/a1/rendition/print%3Aglossy')
    expect(init.method).toBe('DELETE')
  })

  it('rejects when the delete is refused (another user, or already gone)', async () => {
    fetchMock.mockResolvedValue(json(null, 404))
    await expect(removePrintMaster({ uid: 'a1', variantKey: 'print' })).rejects.toThrow('404')
  })

  it('reads the photo’s masters with their measured dimensions', async () => {
    const rows = [{ variant_key: 'print', width: 4032, height: 3024 }]
    fetchMock.mockResolvedValue(json(rows))
    await expect(fetchPrintMasters('a1')).resolves.toEqual(rows)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/attachments/a1/renditions')
  })
})

describe('usePrintMaster', () => {
  beforeEach(() => vi.stubGlobal('fetch', fetchMock))
  afterEach(() => {
    vi.unstubAllGlobals()
    fetchMock.mockReset()
    toastAdd.mockReset()
    printMasterUploads().clear()
    vi.restoreAllMocks()
  })

  it('confirms a stored master, clears the photo’s chip and refreshes the calling view', async () => {
    routeUpload()
    const { composable, invalidate } = mountPrintMaster(['memos'])

    composable.attach({ uid: 'a1', file: master(), variantKey: 'print' })

    await vi.waitFor(() =>
      expect(toastAdd).toHaveBeenCalledWith(
        expect.objectContaining({ severity: 'success', summary: 'Print master attached' }),
      ),
    )
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['memos'] })
    expect(printMasterUploads().get('a1')).toBeUndefined() // the chip goes back to PRINT
  })

  it('invalidates the cache the calling view actually renders', async () => {
    routeUpload()
    const { composable, invalidate } = mountPrintMaster(['attachments'])

    composable.attach({ uid: 'a1', file: master(), variantKey: 'print' })

    await vi.waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['attachments'] }))
  })

  it('publishes the upload as it runs, so the photo’s tile can chip it', async () => {
    let release: ((r: Response) => void) | undefined
    fetchMock.mockImplementation((url: string) => {
      if (url === '/api/uploads') {
        return new Promise<Response>((resolve) => {
          release = resolve
        })
      }
      return Promise.resolve(json(null, 201))
    })
    const { composable } = mountPrintMaster()

    composable.attach({ uid: 'a1', file: master(), variantKey: 'print' })

    await vi.waitFor(() =>
      expect(printMasterUploads().get('a1')).toEqual({ status: 'uploading', progress: 0 }),
    )
    release?.(json({ upload_uid: 'u1', chunk_size: 1 << 20, bytes_received: 10 }))
  })

  it('surfaces a rejection as an error toast, leaves the cache alone and keeps a retryable chip', async () => {
    // The MEMNG-74 bug: the feed's copy had no onError, so a rejected upload was indistinguishable
    // from a stored one — silent failure, stale UI.
    routeUpload(json(null, 500))
    const { composable, invalidate } = mountPrintMaster(['memos'])

    composable.attach({ uid: 'a1', file: master(), variantKey: 'print' })

    await vi.waitFor(() =>
      expect(toastAdd).toHaveBeenCalledWith(
        expect.objectContaining({ severity: 'error', summary: 'Print master upload failed' }),
      ),
    )
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }))
    expect(invalidate).not.toHaveBeenCalled() // nothing was stored — do not refetch as if it were
    expect(printMasterUploads().get('a1')?.status).toBe('error')
  })

  it('retries a failed upload with the very file that failed', async () => {
    routeUpload(json(null, 500))
    const { composable } = mountPrintMaster()
    composable.attach({ uid: 'a1', file: master(), variantKey: 'print:glossy' })
    await vi.waitFor(() => expect(printMasterUploads().get('a1')?.status).toBe('error'))
    fetchMock.mockClear()
    routeUpload()

    composable.retry('a1')

    await vi.waitFor(() =>
      expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' })),
    )
    // ...under the same key, at the same photo — a retry is the same intent, not a new one.
    expect(JSON.parse(fetchMock.mock.calls[2][1].body).variant_key).toBe('print:glossy')
    expect(fetchMock.mock.calls[2][0]).toBe('/api/attachments/a1/renditions')
  })

  it('does nothing when asked to retry a photo with no failed upload', async () => {
    const { composable } = mountPrintMaster()
    composable.retry('nobody')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('confirms a removal and refreshes; a refused removal toasts and refreshes nothing', async () => {
    fetchMock.mockResolvedValue(json(null, 204))
    const { composable, invalidate } = mountPrintMaster(['memos'])

    composable.remove({ uid: 'a1', variantKey: 'print' })

    await vi.waitFor(() =>
      expect(toastAdd).toHaveBeenCalledWith(
        expect.objectContaining({ severity: 'success', summary: 'Print master removed' }),
      ),
    )
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['memos'] })

    toastAdd.mockReset()
    invalidate.mockClear()
    fetchMock.mockResolvedValue(json(null, 404))

    composable.remove({ uid: 'a1', variantKey: 'print' })

    await vi.waitFor(() =>
      expect(toastAdd).toHaveBeenCalledWith(
        expect.objectContaining({
          severity: 'error',
          summary: 'Removing the print master failed',
        }),
      ),
    )
    expect(invalidate).not.toHaveBeenCalled()
  })
})
