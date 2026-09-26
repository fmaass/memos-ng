import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { type Component, h, nextTick, ref } from 'vue'
import { useRoute } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createDockSegment, DOCK_SEGMENT_KEY } from '@/composables/useDockSegment'
import ResourcesView from '@/views/ResourcesView.vue'

const refetch = vi.fn()

// Most cases pin the view's wiring around one already-loaded page (a faked query). The route cases
// flip `real.on`: then the view's REAL query runs through the REAL generated client and only the
// transport underneath is scripted, so the parameters are read where the request is sent.
const { real, transport } = vi.hoisted(() => ({
  real: { on: false },
  transport: vi.fn<(req: Request) => Promise<Response>>(),
}))
vi.mock('@/composables/useApi', async () => {
  const { default: createClient } = await import('openapi-fetch')
  const client = createClient({ baseUrl: '/', fetch: (req: Request) => transport(req) })
  const fake = { GET: vi.fn(), DELETE: vi.fn() }
  return { useApi: () => (real.on ? client : fake) }
})
const { push, replace, route } = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  route: { query: {} as Record<string, string> },
}))
vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const live = reactive(route)
  return { useRouter: () => ({ push, replace }), useRoute: () => live }
})
// The viewer is PhotoSwipe (needs a real DOM) — mock the composable so jsdom doesn't boot it.
const { lightboxOpen } = vi.hoisted(() => ({ lightboxOpen: vi.fn() }))
vi.mock('@/composables/useMediaLightbox', () => ({
  useMediaLightbox: () => ({
    open: lightboxOpen,
    destroy: vi.fn(),
    refreshPrintMaster: refreshPrintMaster,
  }),
}))
// Print masters are a shared composable (Resources lightbox + memo feed), unit-tested in
// composables/__tests__/usePrintMaster.spec.ts; here we only pin the wiring.
const { printAttach, printRemove, printRetry, usePrintMasterMock, refreshPrintMaster } = vi.hoisted(
  () => {
    const printAttach = vi.fn()
    const printRemove = vi.fn()
    const printRetry = vi.fn()
    return {
      printAttach,
      printRemove,
      printRetry,
      refreshPrintMaster: vi.fn(),
      usePrintMasterMock: vi.fn(() => ({
        attach: printAttach,
        remove: printRemove,
        retry: printRetry,
        uploads: new Map(),
      })),
    }
  },
)
vi.mock('@/composables/usePrintMaster', () => ({
  usePrintMaster: usePrintMasterMock,
  fetchPrintMasters: vi.fn().mockResolvedValue([]),
  printMasterUploads: () => new Map(),
}))
// The paged library is exercised for real (transport-level) in ResourcesView.library.spec.ts; here
// one already-loaded page is enough to pin the view's wiring around it.
vi.mock('@tanstack/vue-query', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/vue-query')>()
  return {
    ...actual,
    useQueryClient: (...args: Parameters<typeof actual.useQueryClient>) =>
      real.on ? actual.useQueryClient(...args) : { invalidateQueries: vi.fn() },
    useQuery: (...args: Parameters<typeof actual.useQuery>) =>
      real.on ? actual.useQuery(...args) : fakeTags(),
    useInfiniteQuery: (...args: Parameters<typeof actual.useInfiniteQuery>) =>
      real.on ? actual.useInfiniteQuery(...args) : fakeLibrary(),
  }
})
function fakeTags() {
  return { data: ref(undefined), isError: ref(false), isPending: ref(true) }
}
function fakeLibrary() {
  return {
    data: ref({ pages: [{ nextCursor: undefined, items: [
      {
        uid: 'a1',
        mime: 'image/jpeg',
        status: 'ready',
        blurhash: null,
      sha256: null,
        width: 800,
        height: 600,
        mine: true,
        hasPrint: false,
        orphan: true,
        thumbnailUrl: '/api/attachments/a1/thumb',
        previewUrl: '/api/attachments/a1/mobile',
        webUrl: '/api/attachments/a1/web',
        posterUrl: '/api/attachments/a1/poster',
        downloadUrl: '/api/attachments/a1/original',
        printUrl: '/api/attachments/a1/print',
        memoUid: null, // an orphan upload: it hangs on no memo
        takenAt: '2026-09-02T10:00:00Z',
      },
      {
        uid: 'a2',
        mime: 'image/jpeg',
        status: 'ready',
        blurhash: null,
        sha256: null,
        width: 800,
        height: 600,
        mine: true,
        hasPrint: false,
        orphan: false,
        thumbnailUrl: '/api/attachments/a2/thumb',
        previewUrl: '/api/attachments/a2/mobile',
        webUrl: '/api/attachments/a2/web',
        posterUrl: '/api/attachments/a2/poster',
        downloadUrl: '/api/attachments/a2/original',
        printUrl: '/api/attachments/a2/print',
        memoUid: 'memo-7',
        takenAt: '2026-09-01T10:00:00Z',
      },
    ] }] }),
    isPending: ref(false),
    isFetching: ref(false),
    isError: ref(false),
    isFetchNextPageError: ref(false),
    hasNextPage: ref(false),
    isFetchingNextPage: ref(false),
    fetchNextPage: vi.fn(),
    refetch,
  }
}

describe('ResourcesView', () => {
  afterEach(() => {
    real.on = false
    route.query = {}
    replace.mockClear()
    transport.mockReset()
    vi.restoreAllMocks()
    refetch.mockClear()
    lightboxOpen.mockClear()
    printAttach.mockClear()
    printRemove.mockClear()
    printRetry.mockClear()
    usePrintMasterMock.mockClear()
    push.mockClear()
  })

  it('leaves the viewer for the memo the photo hangs on', () => {
    // Resources is the one gallery that shows everything at once, so a photo found here is often
    // wanted in context; the jump goes to the same permalink the Places viewer uses.
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    wrapper.findComponent({ name: 'JustifiedGallery' }).vm.$emit('open', 1)

    lightboxOpen.mock.calls[0][2].onShowInMemo('memo-7')

    expect(push).toHaveBeenCalledWith('/m/memo-7')
  })

  it('hands the viewer an orphan with no memo to jump to', () => {
    // The action is offered per slide, and the viewer withholds it on one that knows no memo
    // (composables/__tests__/useMediaLightbox.spec.ts) — that guard only holds while this view
    // passes the orphan's empty memoUid through instead of inventing one.
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    wrapper.findComponent({ name: 'JustifiedGallery' }).vm.$emit('open', 0)

    // Both halves, or the case says nothing: with no handler supplied the viewer offers the action
    // on no slide at all, and an empty memoUid would then pass for the wrong reason.
    expect(lightboxOpen.mock.calls[0][2].onShowInMemo).toBeTypeOf('function')
    expect(lightboxOpen.mock.calls[0][0][0].memoUid).toBeNull()
  })

  it('renders the justified gallery + the Library header (not the old grid or inline type filter)', () => {
    // Layout/image sizing needs real DOM width (covered by lib/justified.spec); here we assert the
    // gallery container + header are wired and the legacy grid/tile markup and the inline type
    // SelectButton are gone (the kind moved into the header's options menu).
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    expect(wrapper.find('[data-test="justified-gallery"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="library-header"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="library-options"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="type-filter"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="resource-grid"]').exists()).toBe(false)
  })

  it('files the tiles under Zurich month headers, one gallery per month', () => {
    route.query = {}
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    const headers = wrapper.findAll('[data-test="month-header"]')
    expect(headers.map((h) => h.text())).toEqual(['September 2026'])
    expect(headers[0]!.classes()).toEqual(expect.arrayContaining(['sticky', 'top-[var(--chrome-height,0px)]']))
    expect(wrapper.findAll('[data-test="justified-gallery"]')).toHaveLength(1)
  })

  it('registers its Years | Months | All pill with the shell’s dock, All active, the rest disabled', async () => {
    const registration = createDockSegment()
    const wrapper = mount(ResourcesView, {
      global: {
        plugins: [PrimeVue, ConfirmationService, ToastService],
        provide: { [DOCK_SEGMENT_KEY as symbol]: registration },
      },
    })
    expect(registration.segment.value).not.toBeNull()
    const pill = mount({ render: () => h(registration.segment.value as Component) }, {
      global: { plugins: [PrimeVue] },
    })
    const buttons = pill.findAll('button')
    expect(buttons.map((b) => b.text())).toEqual(['Years', 'Months', 'All'])
    expect(buttons.map((b) => b.attributes('disabled') !== undefined)).toEqual([true, true, false])
    expect(buttons[2]!.attributes('aria-pressed')).toBe('true')
    wrapper.unmount()
    await nextTick()
    expect(registration.segment.value).toBeNull()
  })

  describe('the route query (real query, real client, scripted transport)', () => {
    // Every view mounted here listens to the one shared route: unmount each, or a later case counts
    // an earlier view's requests.
    const mounted: { unmount(): void }[] = []
    afterEach(() => mounted.splice(0).forEach((w) => w.unmount()))
    function mountReal() {
      real.on = true
      transport.mockImplementation(async () =>
        new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } }),
      )
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      const wrapper = mount(ResourcesView, {
        global: {
          plugins: [[VueQueryPlugin, { queryClient }], PrimeVue, ConfirmationService, ToastService],
        },
      })
      mounted.push(wrapper)
      return wrapper
    }
    const listCalls = () =>
      transport.mock.calls
        .map(([req]) => new URL(req.url))
        .filter((u) => u.pathname === '/api/attachments')

    it('reads ?type=video&order=oldest from the route and asks for order=taken_asc&type=video', async () => {
      route.query = { type: 'video', order: 'oldest' }
      mountReal()
      await flushPromises()
      const sent = listCalls()
      expect(sent).toHaveLength(1)
      expect(sent[0]!.searchParams.get('order')).toBe('taken_asc')
      expect(sent[0]!.searchParams.get('type')).toBe('video')
      expect(sent[0]!.searchParams.has('tag')).toBe(false)
    })

    it('reads ?type=video&order=oldest&tag=recap and passes order=taken_asc&type=video&tag=recap', async () => {
      route.query = { type: 'video', order: 'oldest', tag: 'recap' }
      mountReal()
      await flushPromises()
      const sent = listCalls()
      expect(sent).toHaveLength(1)
      expect(sent[0]!.searchParams.get('order')).toBe('taken_asc')
      expect(sent[0]!.searchParams.get('type')).toBe('video')
      expect(sent[0]!.searchParams.get('tag')).toBe('recap')
    })

    it('follows a query-only navigation (back/forward) with a fresh first page', async () => {
      route.query = {}
      mountReal()
      await flushPromises()
      // Through the reactive route the view holds, as the router would update it.
      useRoute().query = { tag: 'recap' }
      await flushPromises()
      const sent = listCalls()
      expect(sent).toHaveLength(2)
      expect(sent[0]!.searchParams.get('order')).toBe('taken_desc')
      expect(sent[1]!.searchParams.get('tag')).toBe('recap')
      expect(sent[1]!.searchParams.has('cursor')).toBe(false)
      expect(replace).not.toHaveBeenCalled() // the URL already says it: no churn
    })

    it('writes a filter picked in the header into the URL', async () => {
      route.query = { type: 'video' }
      const wrapper = mountReal()
      await flushPromises()
      wrapper
        .findComponent({ name: 'LibraryHeader' })
        .vm.$emit('update:filter', { type: 'video', order: 'oldest', tag: 'recap' })
      await flushPromises()
      expect(replace).toHaveBeenCalledWith({ query: { type: 'video', order: 'oldest', tag: 'recap' } })
    })
  })

  it('keeps its own wide gallery column (not the feed reading column)', () => {
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    const classes = wrapper.find('[data-test="view-resources"]').classes()
    expect(classes).toContain('max-w-6xl')
    expect(classes).not.toContain('xl:max-w-4xl')
  })

  it('sends a first print-master attach straight through the shared mutation', () => {
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    wrapper.findComponent({ name: 'JustifiedGallery' }).vm.$emit('open', 0)
    const file = new File(['tiff-bytes'], 'master.tif', { type: 'image/tiff' })

    lightboxOpen.mock.calls[0][2].onAttachPrint({
      uid: 'a1',
      file,
      variantKey: 'print',
      replacing: false,
    })

    expect(usePrintMasterMock).toHaveBeenCalledWith(['attachments']) // this view's own cache
    expect(printAttach).toHaveBeenCalledWith({ uid: 'a1', file, variantKey: 'print' })
  })

  it('retries a failed print-master upload from the gallery tile itself', () => {
    // Orphan attachments live here and have no memo card: if the tile could not retry, a failed
    // upload would be unrecoverable once its toast is gone.
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })

    wrapper.findComponent({ name: 'JustifiedGallery' }).vm.$emit('print-retry', 'a1')

    expect(printRetry).toHaveBeenCalledWith('a1')
  })

  it('holds a REPLACE and a REMOVE behind a confirmation', () => {
    // Both destroy a master that is already there; neither may fire on the click alone.
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    wrapper.findComponent({ name: 'JustifiedGallery' }).vm.$emit('open', 0)
    const handlers = lightboxOpen.mock.calls[0][2]

    handlers.onAttachPrint({
      uid: 'a1',
      file: new File(['x'], 'new.tif', { type: 'image/tiff' }),
      variantKey: 'print:glossy',
      replacing: true,
    })
    handlers.onRemovePrint({ uid: 'a1', variantKey: 'print:glossy' })

    expect(printAttach).not.toHaveBeenCalled()
    expect(printRemove).not.toHaveBeenCalled()
  })

  it('re-mints Resources when gallery retry bubbles, throttled to one refetch per cooldown', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(30_000)
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    wrapper.findComponent({ name: 'JustifiedGallery' }).vm.$emit('retry', 'a1')
    expect(refetch).toHaveBeenCalledTimes(1)
    wrapper.findComponent({ name: 'JustifiedGallery' }).vm.$emit('retry', 'a1')
    expect(refetch).toHaveBeenCalledTimes(1)
  })
})
