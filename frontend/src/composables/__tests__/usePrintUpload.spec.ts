import { QueryClient, type QueryKey, VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { uploadPrintMaster, usePrintUpload } from '@/composables/usePrintUpload'

// PrimeVue's toast is a dependency of the unit, not the unit itself — mock it so the emitted
// messages are inspectable without mounting the <Toast /> outlet.
const { toastAdd } = vi.hoisted(() => ({ toastAdd: vi.fn() }))
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))

const fetchMock = vi.fn()

function res(status: number): Response {
  return { ok: status >= 200 && status < 300, status } as Response
}

function master(): File {
  return new File(['tiff-bytes'], 'master.tif', { type: 'image/tiff' })
}

// Mount a bare host so the composable runs inside a real component + QueryClient context
// (useMutation/useQueryClient inject).
function mountPrintUpload(invalidateKey: QueryKey = ['memos']) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue()
  let mutation!: ReturnType<typeof usePrintUpload>
  const Host = defineComponent({
    setup() {
      mutation = usePrintUpload(invalidateKey)
      return () => h('div')
    },
  })
  const wrapper = mount(Host, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } })
  return { mutation, invalidate, wrapper }
}

describe('uploadPrintMaster', () => {
  beforeEach(() => vi.stubGlobal('fetch', fetchMock))
  afterEach(() => {
    vi.unstubAllGlobals()
    fetchMock.mockReset()
  })

  it('POSTs the file as multipart to the attachment print endpoint with the session cookie', async () => {
    fetchMock.mockResolvedValue(res(204))

    await uploadPrintMaster({ uid: 'a1', file: master() })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/attachments/a1/print')
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('include') // BFF session cookie
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.body as FormData).get('file')).toBeInstanceOf(File)
  })

  it('rejects with the status when the server refuses the upload', async () => {
    fetchMock.mockResolvedValue(res(413))
    await expect(uploadPrintMaster({ uid: 'a1', file: master() })).rejects.toThrow('413')
  })
})

describe('usePrintUpload', () => {
  beforeEach(() => vi.stubGlobal('fetch', fetchMock))
  afterEach(() => {
    vi.unstubAllGlobals()
    fetchMock.mockReset()
    toastAdd.mockReset()
    vi.restoreAllMocks()
  })

  it('confirms a stored master and refreshes the calling view list', async () => {
    fetchMock.mockResolvedValue(res(204))
    const { mutation, invalidate } = mountPrintUpload(['memos'])

    await mutation.mutateAsync({ uid: 'a1', file: master() })

    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success', summary: 'Print version attached' }),
    )
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['memos'] })
  })

  it('invalidates the cache the calling view actually renders', async () => {
    fetchMock.mockResolvedValue(res(204))
    const { mutation, invalidate } = mountPrintUpload(['attachments'])

    await mutation.mutateAsync({ uid: 'a1', file: master() })

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['attachments'] })
  })

  it.each([
    ['4xx', 422],
    ['5xx', 500],
  ])('surfaces a %s rejection as an error toast and leaves the cache alone', async (_label, status) => {
    // The MEMNG-74 bug: the feed's copy had no onError, so a rejected upload was indistinguishable
    // from a stored one — silent failure, stale UI.
    fetchMock.mockResolvedValue(res(status))
    const { mutation, invalidate } = mountPrintUpload(['memos'])

    await expect(mutation.mutateAsync({ uid: 'a1', file: master() })).rejects.toThrow()

    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error', summary: 'Print upload failed' }),
    )
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }))
    expect(invalidate).not.toHaveBeenCalled() // nothing was stored — do not refetch as if it were
  })

  it('surfaces a dropped connection as an error toast too', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    const { mutation, invalidate } = mountPrintUpload(['memos'])

    await expect(mutation.mutateAsync({ uid: 'a1', file: master() })).rejects.toThrow()

    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error' }))
    expect(invalidate).not.toHaveBeenCalled()
  })
})
