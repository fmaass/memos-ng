import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'
import { createDockSegment, DOCK_SEGMENT_KEY } from '@/composables/useDockSegment'
import type { LightboxHandlers } from '@/composables/useMediaLightbox'
import ResourcesView from '@/views/ResourcesView.vue'

// The single delete in the viewer, sent through the app's REAL generated client (`@/api/client`)
// with only the wire underneath scripted. openapi-fetch binds `fetch` when the client is created,
// which is when that module is first imported — so the global is stubbed here, hoisted above every
// import, and nothing in this file substitutes the client.
const { transport } = vi.hoisted(() => {
  const transport = vi.fn<(req: Request) => Promise<Response>>()
  vi.stubGlobal('fetch', (req: Request) => transport(req))
  return { transport }
})
const { confirmRequire, toastAdd, lightboxOpen } = vi.hoisted(() => ({
  confirmRequire: vi.fn(),
  toastAdd: vi.fn(),
  lightboxOpen: vi.fn(),
}))
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: confirmRequire, close: vi.fn() }) }))
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ query: {} }),
}))
// PhotoSwipe needs a real DOM; the viewer's contract is the handlers it is HANDED.
vi.mock('@/composables/useMediaLightbox', () => ({
  useMediaLightbox: () => ({ open: lightboxOpen, destroy: vi.fn(), refreshPrintMaster: vi.fn() }),
}))

function row(uid: string) {
  return {
    uid,
    mime: 'image/jpeg',
    status: 'ready',
    blurhash: null,
    sha256: null,
    width: 800,
    height: 600,
    duration: null,
    mine: true,
    filename: `${uid}.jpg`,
    hasPrint: false,
    hasLegacyPrint: false,
    orphan: true,
    renditions: [],
    thumbnailUrl: `/api/attachments/${uid}/thumb`,
    previewUrl: `/api/attachments/${uid}/mobile`,
    webUrl: `/api/attachments/${uid}/web`,
    posterUrl: `/api/attachments/${uid}/poster`,
    downloadUrl: `/api/attachments/${uid}/original`,
    printUrl: `/api/attachments/${uid}/print`,
    memoUid: null,
    takenAt: '2026-09-02T10:00:00Z',
  }
}

const json = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

// Both empty-bodied 404 shapes openapi-fetch knows: without a Content-Length it reads the body as
// text and hands back `error: ''`; with `Content-Length: 0` it skips the body and hands back
// `error: undefined`. Either is falsy, which is why `error` cannot be what decides.
const EMPTY_404 = {
  'no Content-Length (error is the empty string)': {
    respond: () => new Response(null, { status: 404 }),
    error: '',
  },
  'Content-Length: 0 (error is undefined)': {
    respond: () => new Response(null, { status: 404, headers: { 'Content-Length': '0' } }),
    error: undefined,
  },
}

let deleteAnswer: () => Response
let wrapper: VueWrapper | null = null
let queryClient: QueryClient

async function settle() {
  for (let i = 0; i < 10; i++) await flushPromises()
}

// Mounts the Library and opens the viewer on its first tile; returns the handlers the viewer is handed.
async function openViewer() {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  wrapper = mount(ResourcesView, {
    attachTo: document.body,
    global: {
      plugins: [[VueQueryPlugin, { queryClient }], PrimeVue],
      provide: { [DOCK_SEGMENT_KEY as symbol]: createDockSegment() },
    },
  })
  await settle()
  await wrapper.find('[data-test="tile-open"]').trigger('click')
  return lightboxOpen.mock.calls.at(-1)![2] as Required<LightboxHandlers>
}

// Opens the viewer and accepts its delete confirm.
async function deleteFromViewer(uid: string) {
  const handlers = await openViewer()
  handlers.onDelete(uid)
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  const { header, accept } = confirmRequire.mock.calls.at(-1)![0] as {
    header: string
    accept: () => Promise<void>
  }
  expect(header).toBe('Delete media')
  await accept()
  await settle()
  return invalidate
}

const attachmentInvalidations = (invalidate: Awaited<ReturnType<typeof deleteFromViewer>>) =>
  invalidate.mock.calls.filter(([f]) => JSON.stringify((f as { queryKey?: unknown }).queryKey) === '["attachments"]')
const deletesSent = () =>
  transport.mock.calls.filter(([req]) => req.method === 'DELETE').map(([req]) => new URL(req.url).pathname)

beforeEach(() => {
  transport.mockReset()
  transport.mockImplementation(async (req) => {
    if (req.method === 'DELETE') return deleteAnswer()
    return json(new URL(req.url).pathname === '/api/attachments' ? [row('a1'), row('a2')] : [])
  })
  confirmRequire.mockReset()
  toastAdd.mockReset()
  lightboxOpen.mockReset()
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(private cb: ResizeObserverCallback) {}
      observe() {
        this.cb([{ contentRect: { width: 800 } } as ResizeObserverEntry], this as ResizeObserver)
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
})
afterAll(() => {
  vi.unstubAllGlobals()
})

describe('ResourcesView single delete, judged on the status', () => {
  it.each(Object.entries(EMPTY_404))('a 404 with an empty body, %s, is a failed delete', async (_, shape) => {
    deleteAnswer = shape.respond
    // The shape this case names is what the real client makes of it — else the case proves nothing.
    const direct = await api.DELETE('/api/attachments/{uid}', { params: { path: { uid: 'probe' } } })
    expect(direct.response.status).toBe(404)
    expect(direct.error).toBe(shape.error)
    transport.mockClear()

    const invalidate = await deleteFromViewer('a1')

    expect(deletesSent()).toEqual(['/api/attachments/a1'])
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error', summary: 'Delete failed' }))
    expect(attachmentInvalidations(invalidate)).toEqual([])
  })

  it('a 2xx delete refreshes the library and raises no failure toast', async () => {
    deleteAnswer = () => new Response(null, { status: 204 })

    const invalidate = await deleteFromViewer('a1')

    expect(deletesSent()).toEqual(['/api/attachments/a1'])
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ summary: 'Delete failed' }))
    expect(attachmentInvalidations(invalidate)).toHaveLength(1)
  })
})

// Every viewer confirm whose accept deletes or replaces data opens with focus on Cancel (MEMNG-574);
// ResourcesView.confirm.spec shows, with the real dialog mounted, that this option moves the focus.
describe('ResourcesView viewer confirms open with focus on Cancel', () => {
  beforeEach(() => {
    deleteAnswer = () => new Response(null, { status: 204 })
  })

  it('Delete media', async () => {
    const handlers = await openViewer()
    handlers.onDelete('a1')
    expect(confirmRequire).toHaveBeenCalledTimes(1)
    expect(confirmRequire.mock.calls[0][0]).toMatchObject({ header: 'Delete media', defaultFocus: 'reject' })
  })

  it('Replace print master', async () => {
    const handlers = await openViewer()
    handlers.onAttachPrint({ uid: 'a1', file: new File(['x'], 'print.tif'), variantKey: 'print', replacing: true })
    expect(confirmRequire).toHaveBeenCalledTimes(1)
    expect(confirmRequire.mock.calls[0][0]).toMatchObject({ header: 'Replace print master', defaultFocus: 'reject' })
  })

  it('Remove print master', async () => {
    const handlers = await openViewer()
    handlers.onRemovePrint({ uid: 'a1', variantKey: 'print' })
    expect(confirmRequire).toHaveBeenCalledTimes(1)
    expect(confirmRequire.mock.calls[0][0]).toMatchObject({ header: 'Remove print master', defaultFocus: 'reject' })
  })
})
