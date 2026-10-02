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
  return { data: ref(undefined), isError: ref(false), isPending: ref(true), isSuccess: ref(false), isFetching: ref(false), refetch: vi.fn() }
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

  it('leaves the viewer for the memo the photo hangs on', async () => {
    // Resources is the one gallery that shows everything at once, so a photo found here is often
    // wanted in context; the jump goes to the same permalink the Places viewer uses.
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    await wrapper.find('[data-uid="a2"] [data-test="tile-open"]').trigger('click')

    lightboxOpen.mock.calls[0][2].onShowInMemo('memo-7')

    expect(push).toHaveBeenCalledWith('/m/memo-7')
  })

  it('hands the viewer an orphan with no memo to jump to', async () => {
    // The action is offered per slide, and the viewer withholds it on one that knows no memo
    // (composables/__tests__/useMediaLightbox.spec.ts) — that guard only holds while this view
    // passes the orphan's empty memoUid through instead of inventing one.
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    await wrapper.find('[data-uid="a1"] [data-test="tile-open"]').trigger('click')

    // Both halves, or the case says nothing: with no handler supplied the viewer offers the action
    // on no slide at all, and an empty memoUid would then pass for the wrong reason.
    expect(lightboxOpen.mock.calls[0][2].onShowInMemo).toBeTypeOf('function')
    expect(lightboxOpen.mock.calls[0][0][0].memoUid).toBeNull()
  })

  it('renders the day groups + the Library header (not the old grid or inline type filter)', () => {
    // Layout/image sizing needs real DOM width (covered by lib/justified.spec); here we assert the
    // gallery container + header are wired and the legacy grid/tile markup and the inline type
    // SelectButton are gone (the kind moved into the header's options menu).
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    expect(wrapper.find('[data-test="day-group"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="library-header"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="library-options"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="type-filter"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="resource-grid"]').exists()).toBe(false)
  })

  it('files the tiles under Zurich month headers, one group per Zurich day', () => {
    route.query = {}
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    const headers = wrapper.findAll('[data-test="month-header"]')
    expect(headers.map((h) => h.text())).toEqual(['September 2026'])
    expect(headers[0]!.classes()).toEqual(expect.arrayContaining(['sticky', 'top-[var(--chrome-height,0px)]']))
    expect(wrapper.findAll('[data-test="day-heading"]').map((h) => h.attributes('data-day'))).toEqual([
      '2026-09-02',
      '2026-09-01',
    ])
  })

  it('registers its Years | Months | All pill with the shell’s dock, All active, every segment enabled', async () => {
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
    expect(buttons.map((b) => b.attributes('disabled') !== undefined)).toEqual([false, false, false])
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
    function mountReal(bucketsBody: unknown = [], bucketsStatus = 200) {
      real.on = true
      transport.mockImplementation(async (req) => {
        const buckets = new URL(req.url).pathname === '/api/attachments/buckets'
        return new Response(JSON.stringify(buckets ? bucketsBody : []), {
          status: buckets ? bucketsStatus : 200,
          headers: { 'Content-Type': 'application/json' },
        })
      })
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
    const bucketCalls = () =>
      transport.mock.calls
        .map(([req]) => new URL(req.url))
        .filter((u) => u.pathname === '/api/attachments/buckets')
    const bucket = (key: string, count: number) => ({
      key,
      count,
      coverUid: `c-${key}`,
      coverThumbnailUrl: `/api/attachments/c-${key}/thumb`,
    })

    it('passes ?type=video&tag=recap to the day buckets on Months, in Zurich and with no order', async () => {
      route.query = { type: 'video', order: 'oldest', tag: 'recap', view: 'months' }
      mountReal()
      await flushPromises()
      const sent = bucketCalls()
      expect(sent).toHaveLength(1)
      expect(Object.fromEntries(sent[0]!.searchParams)).toEqual({
        size: 'day',
        tz: 'Europe/Zurich',
        type: 'video',
        tag: 'recap',
      })
      expect(sent[0]!.searchParams.has('order')).toBe(false)
    })

    it('asks for the year buckets on Years, and for the day buckets on All (its day counts)', async () => {
      route.query = { view: 'years' }
      mountReal([bucket('2026', 4)])
      await flushPromises()
      expect(bucketCalls().map((u) => u.searchParams.get('size'))).toEqual(['year'])
      mounted.splice(0).forEach((w) => w.unmount())
      transport.mockClear()

      route.query = {}
      mountReal()
      await flushPromises()
      expect(bucketCalls().map((u) => u.searchParams.get('size'))).toEqual(['day'])
    })

    it('renders Years as cards and Months as the day mosaic, and All keeps its month headers', async () => {
      route.query = { view: 'years' }
      const years = mountReal([bucket('2026', 4), bucket('2025', 1)])
      await flushPromises()
      expect(years.findAll('[data-test="year-card"]')).toHaveLength(2)
      expect(years.find('[data-test="day-group"]').exists()).toBe(false)

      route.query = { view: 'months' }
      const months = mountReal([bucket('2026-09-02', 4), bucket('2026-08-01', 1)])
      await flushPromises()
      expect(months.findAll('[data-test="mosaic-title"]').map((t) => t.text())).toEqual(['Sep 2026', 'Aug 2026'])
    })

    it('does not refetch the list when only the segment changes', async () => {
      route.query = { tag: 'recap' }
      mountReal()
      await flushPromises()
      useRoute().query = { tag: 'recap', view: 'months' }
      await flushPromises()
      useRoute().query = { tag: 'recap', view: 'years' }
      await flushPromises()
      expect(listCalls()).toHaveLength(1)
      expect(replace).not.toHaveBeenCalled()
    })

    it('keeps the segment when the filter changes', async () => {
      route.query = { type: 'video', view: 'months' }
      const wrapper = mountReal()
      await flushPromises()
      wrapper
        .findComponent({ name: 'LibraryHeader' })
        .vm.$emit('update:filter', { type: 'video', order: 'oldest', tag: 'recap' })
      await flushPromises()
      expect(replace).toHaveBeenCalledWith({
        query: { type: 'video', order: 'oldest', tag: 'recap', view: 'months' },
      })
    })

    it('keeps the filter when a year card opens Months, and shows only that year’s months', async () => {
      route.query = { type: 'video', tag: 'recap', view: 'years' }
      const days = [bucket('2027-01-02', 1), bucket('2026-12-31', 2), bucket('2026-06-21', 3), bucket('2025-12-31', 4)]
      const wrapper = mountReal([bucket('2027', 1), bucket('2026', 5), bucket('2025', 4)])
      await flushPromises()
      transport.mockImplementation(async (req) =>
        new Response(JSON.stringify(new URL(req.url).pathname === '/api/attachments/buckets' ? days : []), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
      await wrapper.findAll('[data-test="year-card"]')[1]!.trigger('click')
      await flushPromises()
      expect(replace).toHaveBeenCalledWith({ query: { type: 'video', tag: 'recap', view: 'months' } })
      expect(wrapper.findAll('[data-test="mosaic-title"]').map((t) => t.text())).toEqual(['Dec 2026', 'Jun 2026'])
    })

    it('keeps the filter when a day tile opens All', async () => {
      route.query = { type: 'video', view: 'months' }
      const wrapper = mountReal([bucket('2026-09-02', 4)])
      await flushPromises()
      await wrapper.find('[data-test="mosaic-tile"]').trigger('click')
      await flushPromises()
      expect(replace).toHaveBeenCalledWith({ query: { type: 'video' } })
    })

    it('says so when the buckets fail instead of showing an empty library', async () => {
      route.query = { view: 'months' }
      const wrapper = mountReal({ detail: 'bad' }, 422)
      await flushPromises()
      expect(wrapper.find('[data-test="buckets-error"]').exists()).toBe(true)
      expect(wrapper.find('[data-test="resources-empty"]').exists()).toBe(false)
    })

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

  it('sends a first print-master attach straight through the shared mutation', async () => {
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    await wrapper.find('[data-uid="a1"] [data-test="tile-open"]').trigger('click')
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

    wrapper.findComponent({ name: 'DayGroup' }).vm.$emit('print-retry', 'a1')

    expect(printRetry).toHaveBeenCalledWith('a1')
  })

  it('holds a REPLACE and a REMOVE behind a confirmation', async () => {
    // Both destroy a master that is already there; neither may fire on the click alone.
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    await wrapper.find('[data-uid="a1"] [data-test="tile-open"]').trigger('click')
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
    wrapper.findComponent({ name: 'DayGroup' }).vm.$emit('retry', 'a1')
    expect(refetch).toHaveBeenCalledTimes(1)
    wrapper.findComponent({ name: 'DayGroup' }).vm.$emit('retry', 'a1')
    expect(refetch).toHaveBeenCalledTimes(1)
  })
})
