import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ResourcesView from '@/views/ResourcesView.vue'
import { useRoute } from 'vue-router'

// Unlike ResourcesView.spec, nothing here fakes the query: the view's REAL query runs through the
// REAL generated client and its middlewares, and only the transport underneath is scripted. So a
// request the view never sends, or a header it never reads, fails these cases at the wire.
const { transport } = vi.hoisted(() => ({
  transport: vi.fn<(req: Request) => Promise<Response>>(),
}))
vi.mock('@/composables/useApi', async () => {
  const { default: createClient } = await import('openapi-fetch')
  const { authMiddleware, reachabilityMiddleware } = await import('@/api/client')
  const client = createClient({ baseUrl: '/', fetch: (req: Request) => transport(req) })
  client.use(authMiddleware)
  client.use(reachabilityMiddleware)
  return { useApi: () => client }
})
const { route } = vi.hoisted(() => ({ route: { query: {} as Record<string, string> } }))
vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const live = reactive(route)
  return { useRouter: () => ({ push: vi.fn(), replace: vi.fn() }), useRoute: () => live }
})
// PhotoSwipe needs a real DOM; the viewer's contract is what it is HANDED.
const { lightboxOpen } = vi.hoisted(() => ({ lightboxOpen: vi.fn() }))
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

function ok(body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}
function serverError(): Response {
  return new Response(JSON.stringify({ detail: 'database unavailable' }), {
    status: 503,
    headers: { 'Content-Type': 'application/json' },
  })
}

function sent(n: number): URL {
  return new URL(transport.mock.calls[n]![0].url)
}

let wrapper: VueWrapper | null = null
function mountView() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  wrapper = mount(ResourcesView, {
    attachTo: document.body,
    global: {
      plugins: [[VueQueryPlugin, { queryClient }], PrimeVue, ConfirmationService, ToastService],
    },
  })
  return wrapper
}
async function settle() {
  for (let i = 0; i < 10; i++) await flushPromises()
}
const has = (w: VueWrapper, test: string) => w.find(`[data-test="${test}"]`).exists()

describe('ResourcesView library query', () => {
  beforeEach(() => {
    transport.mockReset()
    lightboxOpen.mockReset()
    // A 5xx makes the reachability middleware probe /health with the global fetch; answer it here
    // so the case never reaches for a real socket.
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 200 })))
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
    route.query = {}
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
  })

  describe('paging (MEMNG-501)', () => {
    beforeEach(() => {
      // happy-dom has no IntersectionObserver: this one reports the sentinel as on screen.
      vi.stubGlobal(
        'IntersectionObserver',
        class {
          constructor(private cb: IntersectionObserverCallback) {}
          observe(target: Element) {
            queueMicrotask(() =>
              this.cb(
                [{ isIntersecting: true, target } as IntersectionObserverEntry],
                this as unknown as IntersectionObserver,
              ),
            )
          }
          disconnect() {}
          unobserve() {}
        },
      )
    })

    it('forwards the X-Next-Cursor of one page into the next request, and stops without it', async () => {
      transport
        .mockResolvedValueOnce(ok([row('p1a'), row('p1b')], { 'X-Next-Cursor': 'cur-p1' }))
        .mockResolvedValueOnce(ok([row('p2a')]))
      const w = mountView()
      await settle()

      expect(transport).toHaveBeenCalledTimes(2)
      expect(sent(0).pathname).toBe('/api/attachments')
      expect(sent(0).searchParams.has('cursor')).toBe(false)
      expect(sent(1).searchParams.get('cursor')).toBe('cur-p1')
      expect(sent(1).searchParams.get('limit')).toBe(sent(0).searchParams.get('limit'))

      // Both pages landed, and the lightbox opened from the LAST tile walks every loaded item.
      w.findComponent({ name: 'JustifiedGallery' }).vm.$emit('open', 2)
      expect(lightboxOpen).toHaveBeenCalledTimes(1)
      const [slides, index] = lightboxOpen.mock.calls[0]!
      expect((slides as { uid: string }[]).map((s) => s.uid)).toEqual(['p1a', 'p1b', 'p2a'])
      expect(index).toBe(2)
    })
  })

  describe('paging without an IntersectionObserver', () => {
    // happy-dom has none, and neither do some embedded browsers: the button is the way on.
    it('loads the next page once however often the button is pressed while it loads', async () => {
      transport.mockResolvedValueOnce(ok([row('p1a')], { 'X-Next-Cursor': 'cur-p1' }))
      let answer!: (r: Response) => void
      transport.mockImplementationOnce(() => new Promise<Response>((r) => (answer = r)))
      const w = mountView()
      await settle()

      // Two presses in the same tick, before a re-render can disable the button: the button's
      // loading state is not the guard, the view's own in-flight check is.
      const button = w.find<HTMLButtonElement>('[data-test="resources-load-more"]').element
      button.click()
      button.click()
      await flushPromises()
      answer(ok([row('p2a')]))
      await settle()

      expect(transport).toHaveBeenCalledTimes(2)
      expect(sent(1).searchParams.get('cursor')).toBe('cur-p1')
      expect(has(w, 'resources-load-more')).toBe(false)
    })

    it('sends an opaque cursor back byte for byte, reserved characters included', async () => {
      const cursor = 'eyJpZCI6MTIzfQ==+/&x=1'
      transport
        .mockResolvedValueOnce(ok([row('p1a')], { 'X-Next-Cursor': cursor }))
        .mockResolvedValueOnce(ok([row('p2a')]))
      const w = mountView()
      await settle()
      await w.find('[data-test="resources-load-more"]').trigger('click')
      await settle()

      expect(sent(1).searchParams.get('cursor')).toBe(cursor)
      expect(sent(1).searchParams.get('x')).toBeNull()
    })

    it('keeps the loaded tiles and offers a retry when a later page fails, without looping', async () => {
      transport
        .mockResolvedValueOnce(ok([row('p1a')], { 'X-Next-Cursor': 'cur-p1' }))
        .mockResolvedValueOnce(serverError())
      const w = mountView()
      await settle()
      await w.find('[data-test="resources-load-more"]').trigger('click')
      await settle()

      expect(transport).toHaveBeenCalledTimes(2)
      expect(has(w, 'justified-gallery')).toBe(true)
      expect(has(w, 'resources-page-error')).toBe(true)
      expect(has(w, 'resources-error')).toBe(false)

      transport.mockResolvedValueOnce(ok([row('p2a')]))
      await w.find('[data-test="resources-page-retry"]').trigger('click')
      await settle()

      expect(transport).toHaveBeenCalledTimes(3)
      expect(sent(2).searchParams.get('cursor')).toBe('cur-p1')
      expect(has(w, 'resources-page-error')).toBe(false)
    })
  })

  describe('paging with the sentinel on screen', () => {
    beforeEach(() => {
      vi.stubGlobal(
        'IntersectionObserver',
        class {
          constructor(private cb: IntersectionObserverCallback) {}
          observe(target: Element) {
            queueMicrotask(() =>
              this.cb(
                [{ isIntersecting: true, target } as IntersectionObserverEntry],
                this as unknown as IntersectionObserver,
              ),
            )
          }
          disconnect() {}
          unobserve() {}
        },
      )
    })

    it('does not re-request a failed page on its own while the sentinel stays visible', async () => {
      // Bounded, so a loop shows up as a count instead of an exhausted worker: after five failures
      // the transport stops answering.
      let failures = 0
      transport
        .mockResolvedValueOnce(ok([row('p1a')], { 'X-Next-Cursor': 'cur-p1' }))
        .mockImplementation(() =>
          ++failures <= 5 ? Promise.resolve(serverError()) : new Promise<Response>(() => {}),
        )
      const w = mountView()
      await settle()
      await settle()

      expect(transport).toHaveBeenCalledTimes(2)
      expect(has(w, 'resources-page-error')).toBe(true)
    })
  })

  describe('errors (MEMNG-504)', () => {
    it('shows an error with a retry, not the empty state, when the first load fails', async () => {
      transport.mockResolvedValueOnce(serverError())
      const w = mountView()
      await settle()

      expect(has(w, 'resources-error')).toBe(true)
      expect(has(w, 'resources-empty')).toBe(false)

      transport.mockResolvedValueOnce(ok([row('a1')]))
      await w.find('[data-test="resources-retry"]').trigger('click')
      await settle()

      expect(transport).toHaveBeenCalledTimes(2)
      expect(has(w, 'resources-error')).toBe(false)
      expect(has(w, 'justified-gallery')).toBe(true)
    })

    it('shows the error state when a filter change fails', async () => {
      transport.mockResolvedValueOnce(ok([row('a1')]))
      const w = mountView()
      await settle()
      expect(has(w, 'justified-gallery')).toBe(true)

      // The kind is picked in the header's options menu now (the inline SelectButton is gone). The
      // menu's tags request comes first, then the list for the new kind — which fails.
      transport.mockResolvedValueOnce(ok([])).mockResolvedValueOnce(serverError())
      await w.find('[data-test="library-options"]').trigger('click')
      await settle()
      document.body.querySelector<HTMLElement>('[data-test="menu-kind-video"]')!.click()
      await settle()

      expect(sent(1).pathname).toBe('/api/attachments/tags')
      expect(sent(2).pathname).toBe('/api/attachments')
      expect(sent(2).searchParams.get('type')).toBe('video')
      expect(has(w, 'resources-error')).toBe(true)
      expect(has(w, 'resources-empty')).toBe(false)
    })

    it('still shows the empty state for a successful empty answer', async () => {
      transport.mockResolvedValueOnce(ok([]))
      const w = mountView()
      await settle()

      expect(has(w, 'resources-empty')).toBe(true)
      expect(has(w, 'resources-error')).toBe(false)
    })
  })

  describe('the jump from a Months day into All (decision 3)', () => {
    // The sentinel is ON SCREEN the whole time, so an unheld sentinel would race the walk for the
    // page after the one holding the day.
    beforeEach(() => {
      vi.stubGlobal(
        'IntersectionObserver',
        class {
          constructor(private cb: IntersectionObserverCallback) {}
          observe(target: Element) {
            queueMicrotask(() =>
              this.cb(
                [{ isIntersecting: true, target } as IntersectionObserverEntry],
                this as unknown as IntersectionObserver,
              ),
            )
          }
          disconnect() {}
          unobserve() {}
        },
      )
    })

    const at = (uid: string, takenAt: string) => ({ ...row(uid), takenAt })
    const dayBucket = (key: string, count: number) => ({
      key,
      count,
      coverUid: `c-${key}`,
      coverThumbnailUrl: `/api/attachments/c-${key}/thumb`,
    })

    it('walks the same list through the page holding the Zurich day, never the page after, and scrolls its first tile into view', async () => {
      route.query = { view: 'months' }
      const listRequests: URL[] = []
      transport.mockImplementation(async (req) => {
        const url = new URL(req.url)
        if (url.pathname === '/api/attachments/buckets') {
          return ok([dayBucket('2026-09-02', 2), dayBucket('2026-09-01', 2)])
        }
        listRequests.push(url)
        const cursor = url.searchParams.get('cursor')
        if (!cursor) return ok([at('p1a', '2026-09-02T12:00:00Z'), at('p1b', '2026-09-02T09:00:00Z')], { 'X-Next-Cursor': 'c1' })
        if (cursor === 'c1') {
          // 22:30Z on 31 August is 00:30 on 1 September in Zurich: the first tile of the 1st.
          return ok([at('target', '2026-08-31T22:30:00Z'), at('p2b', '2026-08-31T12:00:00Z')], { 'X-Next-Cursor': 'c2' })
        }
        return ok([at('p3a', '2026-08-30T10:00:00Z')])
      })
      const scrolled: { uid: string | undefined; requests: number }[] = []
      vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(function (this: Element) {
        scrolled.push({ uid: (this as HTMLElement).dataset.uid, requests: listRequests.length })
      })

      const w = mountView()
      await settle()
      expect(listRequests).toHaveLength(1)
      const tiles = w.findAll('[data-test="mosaic-tile"]')
      expect(tiles.map((t) => t.find('[data-test="mosaic-day"]').text())).toEqual(['1', '2'])

      await tiles[0]!.trigger('click')
      await vi.waitFor(() => expect(scrolled).toHaveLength(1))

      // The receiver is the real tile of that uid, and when it scrolled only the two pages the walk
      // needed had been asked for, the second with the first's cursor and the same limit.
      expect(scrolled[0]).toEqual({ uid: 'target', requests: 2 })
      expect(listRequests[1]!.searchParams.get('cursor')).toBe('c1')
      expect(listRequests.slice(0, 2).map((u) => u.searchParams.get('limit'))).toEqual(['500', '500'])
      const receiver = vi.mocked(Element.prototype.scrollIntoView).mock.contexts[0] as HTMLElement
      expect(receiver.closest('[data-test="justified-gallery"]')).not.toBeNull()
      expect(receiver.matches('[data-test="media-tile"]')).toBe(true)
    })

    it('stops a superseded walk: it asks for no further page, and only the newer jump scrolls and releases', async () => {
      // The sentinel stays OFF screen here, so every list request after the first page is a walk's.
      const sentinelObserves: Element[] = []
      vi.stubGlobal(
        'IntersectionObserver',
        class {
          constructor(private cb: IntersectionObserverCallback) {}
          observe(target: Element) {
            sentinelObserves.push(target)
            queueMicrotask(() =>
              this.cb(
                [{ isIntersecting: false, target } as IntersectionObserverEntry],
                this as unknown as IntersectionObserver,
              ),
            )
          }
          disconnect() {}
          unobserve() {}
        },
      )
      route.query = { view: 'months' }
      let release!: () => void
      const held = new Promise<void>((r) => (release = r))
      const listRequests: URL[] = []
      transport.mockImplementation(async (req) => {
        const url = new URL(req.url)
        if (url.pathname === '/api/attachments/buckets') {
          return ok([dayBucket('2026-09-02', 1), dayBucket('2026-08-01', 1)])
        }
        listRequests.push(url)
        const cursor = url.searchParams.get('cursor')
        if (!cursor) return ok([at('p1a', '2026-09-02T12:00:00Z')], { 'X-Next-Cursor': 'c1' })
        if (cursor === 'c1') {
          await held
          return ok([at('p2a', '2026-08-20T12:00:00Z')], { 'X-Next-Cursor': 'c2' })
        }
        return ok([at('aug', '2026-08-01T10:00:00Z')])
      })
      const scrolled: (string | undefined)[] = []
      vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(function (this: Element) {
        scrolled.push((this as HTMLElement).dataset.uid)
      })

      const w = mountView()
      await settle()
      // First jump: to 1 August, two pages away; its first page (c1) stays on the wire.
      await w.findAll('[data-test="mosaic-tile"]')[1]!.trigger('click')
      await settle()
      expect(listRequests.map((u) => u.searchParams.get('cursor'))).toEqual([null, 'c1'])
      expect(has(w, 'day-jump-spinner')).toBe(true)

      // Back to Months through the pill, then a second jump to a day already loaded.
      useRoute().query = { view: 'months' }
      await settle()
      await w.findAll('[data-test="mosaic-tile"]')[0]!.trigger('click')
      await vi.waitFor(() => expect(scrolled).toEqual(['p1a']))
      await settle()
      expect(has(w, 'day-jump-spinner')).toBe(false)
      const observesAfterSecond = sentinelObserves.length

      // The superseded walk's page lands now: it must stop there.
      release()
      await settle()
      await new Promise((r) => setTimeout(r, 50))
      await settle()
      expect(listRequests.map((u) => u.searchParams.get('cursor'))).toEqual([null, 'c1'])
      expect(scrolled).toEqual(['p1a'])
      expect(has(w, 'day-jump-spinner')).toBe(false)
      expect(sentinelObserves).toHaveLength(observesAfterSecond) // no second release, no re-arm
    })

    it('shows a spinner while it walks', async () => {
      route.query = { view: 'months' }
      let release!: () => void
      const held = new Promise<void>((r) => (release = r))
      transport.mockImplementation(async (req) => {
        const url = new URL(req.url)
        if (url.pathname === '/api/attachments/buckets') return ok([dayBucket('2026-08-01', 1)])
        if (!url.searchParams.get('cursor')) return ok([at('p1a', '2026-09-02T12:00:00Z')], { 'X-Next-Cursor': 'c1' })
        await held
        return ok([at('aug', '2026-08-01T10:00:00Z')])
      })
      vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
      const w = mountView()
      await settle()
      await w.find('[data-test="mosaic-tile"]').trigger('click')
      await settle()
      expect(has(w, 'day-jump-spinner')).toBe(true)
      release()
      await vi.waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled())
      await settle()
      expect(has(w, 'day-jump-spinner')).toBe(false)
    })
  })
})
