import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { useRoute } from 'vue-router'

import ResourcesView from '@/views/ResourcesView.vue'

// The All view by day (plan decision 13), through the view's REAL queries and the REAL generated
// client; only the transport is scripted — the list on `transport`, the day buckets (the headings'
// counts and the Months mosaic) on `dayBuckets`.
const { transport, dayBuckets } = vi.hoisted(() => ({
  transport: vi.fn<(req: Request) => Promise<Response>>(),
  dayBuckets: vi.fn<(req: Request) => Promise<Response>>(),
}))
vi.mock('@/composables/useApi', async () => {
  const { default: createClient } = await import('openapi-fetch')
  const { authMiddleware, reachabilityMiddleware } = await import('@/api/client')
  const isDayBuckets = (req: Request) => {
    const url = new URL(req.url)
    return url.pathname === '/api/attachments/buckets' && url.searchParams.get('size') === 'day'
  }
  const client = createClient({
    baseUrl: '/',
    fetch: (req: Request) => (isDayBuckets(req) ? dayBuckets(req) : transport(req)),
  })
  client.use(authMiddleware)
  client.use(reachabilityMiddleware)
  return { useApi: () => client }
})
const { route, routerReplace } = vi.hoisted(() => ({
  route: { query: {} as Record<string, string> },
  routerReplace: vi.fn(),
}))
vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const live = reactive(route)
  return { useRouter: () => ({ push: vi.fn(), replace: routerReplace }), useRoute: () => live }
})
const { lightboxOpen } = vi.hoisted(() => ({ lightboxOpen: vi.fn() }))
vi.mock('@/composables/useMediaLightbox', () => ({
  useMediaLightbox: () => ({ open: lightboxOpen, destroy: vi.fn(), refreshPrintMaster: vi.fn() }),
}))

function row(uid: string, takenAt: string) {
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
    takenAt,
  }
}
const bucket = (key: string, count: number) => ({
  key,
  count,
  coverUid: `c-${key}`,
  coverThumbnailUrl: `/api/attachments/c-${key}/thumb`,
})
function ok(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}
function serverError(): Response {
  return new Response(JSON.stringify({ detail: 'database unavailable' }), {
    status: 503,
    headers: { 'Content-Type': 'application/json' },
  })
}

// Newest first, as the server sends it. 2026-09-26 is a Saturday, the 25th a Friday, 1 September a
// Tuesday and 30 August a Sunday. 22:30Z on 31 August is 00:30 on 1 September in Zurich.
const LIST = [
  row('s1', '2026-09-26T10:00:00Z'),
  row('s2', '2026-09-26T08:00:00Z'),
  row('f1', '2026-09-25T12:00:00Z'),
  row('f2', '2026-09-25T11:00:00Z'),
  row('f3', '2026-09-25T10:00:00Z'),
  row('t1', '2026-08-31T22:30:00Z'),
  row('a1', '2026-08-30T12:00:00Z'),
  row('a2', '2026-08-30T11:00:00Z'),
  row('a3', '2026-08-30T10:00:00Z'),
  row('a4', '2026-08-30T09:00:00Z'),
]

let wrapper: VueWrapper | null = null
function mountView(queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  wrapper = mount(ResourcesView, {
    attachTo: document.body,
    global: { plugins: [[VueQueryPlugin, { queryClient }], PrimeVue, ConfirmationService, ToastService] },
  })
  return wrapper
}
async function settle() {
  for (let i = 0; i < 10; i++) await flushPromises()
}
const headings = (w: VueWrapper) => w.findAll('[data-test="day-heading"]')
const countOf = (w: VueWrapper, day: string) => w.find(`[data-day="${day}"] [data-test="day-count"]`).text()

// The ResizeObservers the view makes, so a case can report a new size on one.
let observers: { cb: ResizeObserverCallback; targets: Element[]; disconnected: boolean }[] = []

describe('ResourcesView All by day (decision 13)', () => {
  beforeEach(() => {
    transport.mockReset()
    transport.mockImplementation(async (req) => ok(new URL(req.url).pathname === '/api/attachments' ? LIST : []))
    dayBuckets.mockReset()
    dayBuckets.mockImplementation(async () => ok([]))
    lightboxOpen.mockReset()
    routerReplace.mockReset()
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 200 })))
    observers = []
    vi.stubGlobal(
      'ResizeObserver',
      class {
        private entry: { cb: ResizeObserverCallback; targets: Element[]; disconnected: boolean }
        constructor(cb: ResizeObserverCallback) {
          this.entry = { cb, targets: [], disconnected: false }
          observers.push(this.entry)
        }
        observe(target: Element) {
          this.entry.targets.push(target)
          this.entry.cb([{ contentRect: { width: 800 } } as ResizeObserverEntry], this as unknown as ResizeObserver)
        }
        disconnect() {
          this.entry.disconnected = true
        }
        unobserve() {}
      },
    )
  })
  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    route.query = {}
    document.body.innerHTML = ''
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  describe('day groups', () => {
    it('groups the list by Zurich day, newest first, under the month headers', async () => {
      const w = mountView()
      await settle()
      expect(w.findAll('[data-test="month-header"]').map((h) => h.text())).toEqual(['September 2026', 'August 2026'])
      expect(headings(w).map((h) => h.attributes('data-day'))).toEqual([
        '2026-09-26',
        '2026-09-25',
        '2026-09-01',
        '2026-08-30',
      ])
      expect(
        w.findAll('[data-test="day-group"]').map((g) => g.findAll('[data-uid]').map((t) => t.attributes('data-uid'))),
      ).toEqual([['s1', 's2'], ['f1', 'f2', 'f3'], ['t1'], ['a1', 'a2', 'a3', 'a4']])
      // The September header's section holds the first three days, August's the last.
      const [sep, aug] = w.findAll('[data-test="month-header"]').map((h) => h.element.parentElement!)
      expect(sep!.querySelectorAll('[data-test="day-group"]')).toHaveLength(3)
      expect(aug!.querySelectorAll('[data-test="day-group"]')).toHaveLength(1)
    })

    it('names each day in its heading: the weekday, then the date', async () => {
      const w = mountView()
      await settle()
      const first = headings(w)[0]!
      expect(first.find('.font-bold').text()).toBe('Sat')
      expect(first.text()).toContain('26 Sep')
      expect(headings(w).map((h) => h.findAll('span').slice(0, 2).map((s) => s.text()))).toEqual([
        ['Sat', '26 Sep'],
        ['Fri', '25 Sep'],
        ['Tue', '1 Sep'],
        ['Sun', '30 Aug'],
      ])
    })

    it('lays each day out by the frame cycle, one step later per day, counted across the month boundary', async () => {
      const w = mountView()
      await settle()
      const slots = w
        .findAll('[data-test="day-group"]')
        .map((g) => g.findAll('[data-slot]').map((t) => t.attributes('data-slot')))
      expect(slots).toEqual([
        ['wide', 'wide'], // day 0, two pictures: wide frames only
        ['square', 'square', 'square'], // day 1 opens on B
        ['wide'], // day 2, one picture
        // Day 3 (the first of August) opens on the SECOND B, then D takes the single leftover; had
        // the count restarted at the month it would open on A (hero-left, side-right, side-right).
        ['square', 'square', 'square', 'wide'],
      ])
    })

    it('opens the viewer on the whole loaded list at the tapped tile', async () => {
      const w = mountView()
      await settle()
      await w.find('[data-uid="a2"] [data-test="tile-open"]').trigger('click')
      expect(lightboxOpen).toHaveBeenCalledTimes(1)
      const [slides, index] = lightboxOpen.mock.calls[0]!
      expect((slides as { uid: string }[]).map((s) => s.uid)).toEqual(LIST.map((r) => r.uid))
      expect(index).toBe(7)
    })
  })

  describe('day counts', () => {
    it("counts the whole day from the buckets, not the day's loaded fragment", async () => {
      // The list holds two of the 26th; the server has five that day.
      dayBuckets.mockImplementation(async () =>
        ok([bucket('2026-09-26', 5), bucket('2026-09-25', 3), bucket('2026-08-30', 4)]),
      )
      const w = mountView()
      await settle()
      expect(countOf(w, '2026-09-26')).toBe('5')
      expect(countOf(w, '2026-09-25')).toBe('3')
      expect(countOf(w, '2026-08-30')).toBe('4')
      // A day the buckets do not name shows no count rather than a guess.
      expect(countOf(w, '2026-09-01')).toBe('')
    })

    it('leaves every count empty while the buckets are pending', async () => {
      dayBuckets.mockImplementation(() => new Promise<Response>(() => {}))
      const w = mountView()
      await settle()
      expect(headings(w)).toHaveLength(4)
      expect(w.findAll('[data-test="day-count"]').map((c) => c.text())).toEqual(['', '', '', ''])
    })

    it('leaves every count empty when the buckets fail', async () => {
      dayBuckets.mockImplementation(async () => serverError())
      const w = mountView()
      await settle()
      expect(headings(w)).toHaveLength(4)
      expect(w.findAll('[data-test="day-count"]').map((c) => c.text())).toEqual(['', '', '', ''])
    })

    it('shows the undated group with no count, whatever the buckets say', async () => {
      transport.mockImplementation(async () => ok([row('u1', 'not-a-date')]))
      dayBuckets.mockImplementation(async () => ok([bucket('undated', 9)]))
      const w = mountView()
      await settle()
      expect(headings(w).map((h) => h.attributes('data-day'))).toEqual(['undated'])
      expect(headings(w)[0]!.text()).toContain('Undated')
      expect(countOf(w, 'undated')).toBe('')
    })
  })

  describe('the month header measurement', () => {
    it('measures and observes the header when All mounts on an already cached list', async () => {
      const realRect = Element.prototype.getBoundingClientRect
      vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
        if (this.matches('[data-test="month-header"]')) return new DOMRect(0, 0, 390, 44)
        return realRect.call(this)
      })
      // A first visit fills the cache; the second mounts on it, so nothing the view watches changes
      // after mount — the header must still be measured from the first render.
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
      mountView(queryClient)
      await settle()
      wrapper!.unmount()
      wrapper = null
      observers = []
      const requestsBefore = transport.mock.calls.length

      const w = mountView(queryClient)
      await settle()
      expect(transport.mock.calls.length).toBe(requestsBefore) // served from the cache
      const header = w.find('[data-test="month-header"]').element
      expect(observers.some((o) => o.targets.includes(header) && !o.disconnected)).toBe(true)
      expect(
        (w.find('[data-test="view-resources"]').element as HTMLElement).style.getPropertyValue('--month-header-height'),
      ).toBe('44px')
    })

    it('follows the first month header while All shows and lets go of it when All is left', async () => {
      let headerHeight = 44
      const realRect = Element.prototype.getBoundingClientRect
      vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
        if (this.matches('[data-test="month-header"]')) return new DOMRect(0, 0, 390, headerHeight)
        return realRect.call(this)
      })
      const w = mountView()
      await settle()
      const view = () => (w.find('[data-test="view-resources"]').element as HTMLElement).style
      expect(view().getPropertyValue('--month-header-height')).toBe('44px')

      const header = w.find('[data-test="month-header"]').element
      const watching = observers.find((o) => o.targets.includes(header))!
      expect(watching).toBeDefined()
      headerHeight = 60
      watching.cb([], {} as ResizeObserver)
      await nextTick()
      expect(view().getPropertyValue('--month-header-height')).toBe('60px')

      useRoute().query = { view: 'months' }
      await settle()
      expect(watching.disconnected).toBe(true)
    })
  })

  describe('the date pill', () => {
    // happy-dom has no layout: the month header measures 44 px, and each day heading sits where
    // `tops` puts it (a heading not named there is far below the fold).
    let tops: Record<string, number>
    beforeEach(() => {
      tops = {}
      const realRect = Element.prototype.getBoundingClientRect
      vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
        if (this.matches('[data-test="month-header"]')) return new DOMRect(0, 0, 390, 44)
        const day = (this as HTMLElement).dataset.day
        if (day !== undefined) return new DOMRect(0, tops[day] ?? 10_000, 390, 24)
        return realRect.call(this)
      })
    })
    const scroll = async () => {
      document.dispatchEvent(new Event('scroll'))
      await nextTick()
    }
    const pill = (w: VueWrapper) => w.find('[data-test="date-pill"]')

    it('names the day whose heading last reached the line under the header', async () => {
      const w = mountView()
      await settle()
      expect(pill(w).exists()).toBe(false)
      tops = { '2026-09-26': -300, '2026-09-25': 44, '2026-09-01': 45 }
      await scroll()
      expect(pill(w).exists()).toBe(true)
      expect(pill(w).element.tagName).toBe('BUTTON')
      // 44 is exactly the line (chrome 0 + month header 44): reached; 45 is not.
      expect(pill(w).text()).toBe('Fri · 25 Sep 2026')
    })

    it('stays hidden while no heading has reached the line', async () => {
      const w = mountView()
      await settle()
      tops = { '2026-09-26': 45, '2026-09-25': 400 }
      await scroll()
      expect(pill(w).exists()).toBe(false)
    })

    it('hides exactly one second after the LAST scroll event', async () => {
      const w = mountView()
      await settle()
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
      tops = { '2026-09-26': 0 }
      await scroll()
      expect(pill(w).text()).toBe('Sat · 26 Sep 2026')
      vi.advanceTimersByTime(999)
      await nextTick()
      expect(pill(w).exists()).toBe(true)
      vi.advanceTimersByTime(1)
      await nextTick()
      expect(pill(w).exists()).toBe(false)

      // A scroll restarts the second: 600 ms after the first, a second scroll keeps it up past 1000.
      await scroll()
      vi.advanceTimersByTime(600)
      await scroll()
      vi.advanceTimersByTime(600)
      await nextTick()
      expect(pill(w).exists()).toBe(true)
      vi.advanceTimersByTime(399)
      await nextTick()
      expect(pill(w).exists()).toBe(true)
      vi.advanceTimersByTime(1)
      await nextTick()
      expect(pill(w).exists()).toBe(false)
    })

    it('stops listening to the document when the view goes', async () => {
      const add = vi.spyOn(document, 'addEventListener')
      const remove = vi.spyOn(document, 'removeEventListener')
      mountView()
      await settle()
      const added = add.mock.calls.find(([type]) => type === 'scroll')
      expect(added?.[2]).toEqual({ passive: true })
      wrapper!.unmount()
      wrapper = null
      expect(remove.mock.calls.some(([type, fn]) => type === 'scroll' && fn === added![1])).toBe(true)
    })

    it("opens Months at the pill day's month and scrolls that month's section to the top", async () => {
      dayBuckets.mockImplementation(async () =>
        ok([bucket('2026-09-26', 2), bucket('2026-09-25', 3), bucket('2026-08-30', 4)]),
      )
      const scrolled: { month: string | undefined; margin: string; options: unknown }[] = []
      vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(function (this: Element, options?: unknown) {
        const el = this as HTMLElement
        scrolled.push({ month: el.dataset.month, margin: el.style.getPropertyValue('scroll-margin-top'), options })
      })
      const scrollTo = vi.fn()
      vi.stubGlobal('scrollTo', scrollTo)
      const w = mountView()
      await settle()
      tops = { '2026-09-26': -900, '2026-09-25': -400, '2026-09-01': -100, '2026-08-30': 20 }
      await scroll()
      expect(pill(w).text()).toBe('Sun · 30 Aug 2026')

      await pill(w).trigger('click')
      await vi.waitFor(() => expect(scrolled).toHaveLength(1))
      expect(routerReplace).toHaveBeenLastCalledWith({ query: { view: 'months' } })
      expect(w.findAll('[data-test="mosaic-title"]').map((t) => t.text())).toEqual(['Sep 2026', 'Aug 2026'])
      expect(scrolled[0]).toEqual({ month: '2026-08', margin: 'var(--chrome-height, 0px)', options: { block: 'start' } })
      const receiver = vi.mocked(Element.prototype.scrollIntoView).mock.contexts[0] as HTMLElement
      expect(receiver.matches('[data-test="mosaic-section"]')).toBe(true)
      expect(scrollTo).not.toHaveBeenCalled()
    })

    it('lands at the top of Months when it does not show that month, without throwing', async () => {
      dayBuckets.mockImplementation(async () => ok([bucket('2026-08-30', 4)]))
      const intoView = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
      const scrollTo = vi.fn()
      vi.stubGlobal('scrollTo', scrollTo)
      const w = mountView()
      await settle()
      tops = { '2026-09-26': 0 }
      await scroll()
      await pill(w).trigger('click')
      await vi.waitFor(() => expect(scrollTo).toHaveBeenCalledWith({ top: 0 }))
      expect(intoView).not.toHaveBeenCalled()
    })

    it('drops the target when the buckets fail: Months shows its error, nothing scrolls', async () => {
      dayBuckets.mockImplementation(async () => serverError())
      const intoView = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
      const scrollTo = vi.fn()
      vi.stubGlobal('scrollTo', scrollTo)
      const w = mountView()
      await settle()
      tops = { '2026-09-26': 0 }
      await scroll()
      await pill(w).trigger('click')
      await settle()
      await new Promise((r) => setTimeout(r, 50))
      expect(w.find('[data-test="buckets-error"]').exists()).toBe(true)
      expect(intoView).not.toHaveBeenCalled()
      expect(scrollTo).not.toHaveBeenCalled()
    })

    it('cancels a landing still waiting for the buckets when the view changes', async () => {
      let answer!: () => void
      dayBuckets.mockImplementation(
        () => new Promise<Response>((resolve) => (answer = () => resolve(ok([bucket('2026-09-26', 2)])))),
      )
      const intoView = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
      const scrollTo = vi.fn()
      vi.stubGlobal('scrollTo', scrollTo)
      const w = mountView()
      await settle()
      tops = { '2026-09-26': 0 }
      await scroll()
      await pill(w).trigger('click')
      await settle()
      // Back to All before the buckets answer: the Months landing must not happen later.
      useRoute().query = {}
      await settle()
      answer()
      await settle()
      await new Promise((r) => setTimeout(r, 50))
      expect(intoView).not.toHaveBeenCalled()
      expect(scrollTo).not.toHaveBeenCalled()
    })
  })
})
