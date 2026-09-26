import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ResourcesView from '@/views/ResourcesView.vue'

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
vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const route = reactive({ query: {} })
  return { useRouter: () => ({ push: vi.fn(), replace: vi.fn() }), useRoute: () => route }
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
})
