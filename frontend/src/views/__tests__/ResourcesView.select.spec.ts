import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ToastService from 'primevue/toastservice'
import { type Component, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createDockSegment, DOCK_SEGMENT_KEY, type DockSegmentRegistration } from '@/composables/useDockSegment'
import ResourcesView from '@/views/ResourcesView.vue'

// The view's REAL list query and REAL delete run through the REAL generated client; only the
// transport under it is scripted. The confirm service is captured so a case can accept it.
const { transport, confirmRequire, confirmClose, lightboxOpen } = vi.hoisted(() => ({
  transport: vi.fn<(req: Request) => Promise<Response>>(),
  confirmRequire: vi.fn(),
  confirmClose: vi.fn(),
  lightboxOpen: vi.fn(),
}))
vi.mock('@/composables/useApi', async () => {
  const { default: createClient } = await import('openapi-fetch')
  const client = createClient({ baseUrl: '/', fetch: (req: Request) => transport(req) })
  return { useApi: () => client }
})
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: confirmRequire, close: confirmClose }) }))
const { route } = vi.hoisted(() => ({ route: { query: {} as Record<string, string> } }))
vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const live = reactive(route)
  return { useRouter: () => ({ push: vi.fn(), replace: vi.fn() }), useRoute: () => live }
})
vi.mock('@/composables/useMediaLightbox', () => ({
  useMediaLightbox: () => ({ open: lightboxOpen, destroy: vi.fn(), refreshPrintMaster: vi.fn() }),
}))

function row(uid: string, mine = true) {
  return {
    uid,
    mime: 'image/jpeg',
    status: 'ready',
    blurhash: null,
    sha256: null,
    width: 800,
    height: 600,
    duration: null,
    mine,
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
const ok = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

let listed = [row('a'), row('b'), row('f', false)]
const sent = () => transport.mock.calls.map(([req]) => `${req.method} ${new URL(req.url).pathname}`)
const deletes = () => sent().filter((s) => s.startsWith('DELETE'))

let wrapper: VueWrapper | null = null
let registration: DockSegmentRegistration
let queryClient: QueryClient
function mountView() {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  registration = createDockSegment()
  wrapper = mount(ResourcesView, {
    attachTo: document.body,
    global: {
      plugins: [[VueQueryPlugin, { queryClient }], PrimeVue, ToastService],
      provide: { [DOCK_SEGMENT_KEY as symbol]: registration },
    },
  })
  return wrapper
}
async function settle() {
  for (let i = 0; i < 10; i++) await flushPromises()
}
// The toolbar the view hands the dock, mounted the way the dock would render it.
function dockBar() {
  const bar = registration.override.value
  if (!bar) return null
  return mount(bar as Component, { attachTo: document.body })
}
const tile = (w: VueWrapper, uid: string) => w.find(`[data-uid="${uid}"] [data-test="tile-select"]`)
async function enterSelect(w: VueWrapper) {
  await w.find('[data-test="library-select"]').trigger('click')
  await nextTick()
}
function lastConfirm() {
  return confirmRequire.mock.calls.at(-1)![0] as {
    header: string
    accept: () => Promise<void>
    reject: () => void
    onHide: () => void
  }
}

describe('ResourcesView select mode', () => {
  beforeEach(() => {
    listed = [row('a'), row('b'), row('f', false)]
    transport.mockReset()
    // The list answers the loaded rows; every other read (buckets, tags) answers empty.
    transport.mockImplementation(async (req) => {
      if (req.method === 'DELETE') return new Response(null, { status: 204 })
      return ok(new URL(req.url).pathname === '/api/attachments' ? listed : [])
    })
    confirmRequire.mockReset()
    confirmClose.mockReset()
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
    route.query = {}
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    Reflect.deleteProperty(navigator, 'canShare')
    Reflect.deleteProperty(navigator, 'share')
  })

  it('the Select capsule is live in All and disabled in Years and Months', async () => {
    const w = mountView()
    await settle()
    expect(w.find('[data-test="library-select"]').attributes('disabled')).toBeUndefined()
    useRoute().query = { view: 'years' }
    await settle()
    expect(w.find('[data-test="library-select"]').attributes('disabled')).toBeDefined()
  })

  it('in select mode a tile tap selects instead of opening the viewer, and the dock becomes the toolbar', async () => {
    const w = mountView()
    await settle()
    expect(registration.override.value).toBeNull()
    await enterSelect(w)
    await tile(w, 'a').trigger('click')
    expect(lightboxOpen).not.toHaveBeenCalled()
    expect(tile(w, 'a').attributes('aria-pressed')).toBe('true')
    const bar = dockBar()!
    expect(bar.find('[data-test="selection-count"]').text()).toBe('1 Item Selected')
    expect(bar.find('[data-test="selection-delete"]').attributes('aria-label')).toBe('Delete 1 item')
  })

  it('the header toggle selects every loaded item of mine, never a foreign one, and clears on the second tap', async () => {
    const w = mountView()
    await settle()
    await enterSelect(w)
    const toggle = () => w.find('[data-test="library-select-all"]')
    expect(toggle().attributes('aria-pressed')).toBe('false')
    await toggle().trigger('click')
    expect(tile(w, 'a').attributes('aria-pressed')).toBe('true')
    expect(tile(w, 'b').attributes('aria-pressed')).toBe('true')
    expect(tile(w, 'f').attributes('aria-pressed')).toBe('false')
    expect(tile(w, 'f').attributes('disabled')).toBeDefined()
    expect(toggle().attributes('aria-pressed')).toBe('true')
    await toggle().trigger('click')
    expect(w.findAll('[data-test="tile-select"][aria-pressed="true"]')).toHaveLength(0)
    expect(toggle().attributes('aria-pressed')).toBe('false')
  })

  it('the close button leaves select mode and clears the selection', async () => {
    const w = mountView()
    await settle()
    await enterSelect(w)
    await tile(w, 'a').trigger('click')
    await w.find('[data-test="library-select-close"]').trigger('click')
    expect(w.find('[data-test="tile-select"]').exists()).toBe(false)
    expect(registration.override.value).toBeNull()
    await enterSelect(w)
    expect(tile(w, 'a').attributes('aria-pressed')).toBe('false')
  })

  it('Escape leaves select mode', async () => {
    const w = mountView()
    await settle()
    await enterSelect(w)
    await tile(w, 'a').trigger('click')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(w.find('[data-test="tile-select"]').exists()).toBe(false)
  })

  it.each([
    ['leaving All', { view: 'months' }],
    ['changing the kind', { type: 'video' }],
    ['changing the tag', { tag: 'recap' }],
  ])('%s exits select mode, clears it and dismisses a pending delete confirm', async (_, query) => {
    const w = mountView()
    await settle()
    await enterSelect(w)
    await tile(w, 'a').trigger('click')
    await dockBar()!.find('[data-test="selection-delete"]').trigger('click')
    expect(confirmRequire).toHaveBeenCalledTimes(1)
    useRoute().query = query
    await settle()
    expect(confirmClose).toHaveBeenCalledTimes(1)
    expect(registration.override.value).toBeNull()
    expect(w.find('[data-test="library-select-all"]').exists()).toBe(false)
    // Even an accept that somehow still arrived must not delete from a selection that was left.
    await lastConfirm().accept()
    await settle()
    expect(deletes()).toEqual([])
  })

  it('navigating away (unmount) withdraws the toolbar and dismisses a pending confirm', async () => {
    const w = mountView()
    await settle()
    await enterSelect(w)
    await tile(w, 'a').trigger('click')
    await dockBar()!.find('[data-test="selection-delete"]').trigger('click')
    w.unmount()
    wrapper = null
    expect(confirmClose).toHaveBeenCalledTimes(1)
    expect(registration.override.value).toBeNull()
  })

  it('a dismissed confirm is not closed again by a later exit', async () => {
    const w = mountView()
    await settle()
    await enterSelect(w)
    await tile(w, 'a').trigger('click')
    await dockBar()!.find('[data-test="selection-delete"]').trigger('click')
    lastConfirm().reject()
    lastConfirm().onHide()
    await w.find('[data-test="library-select-close"]').trigger('click')
    expect(confirmClose).not.toHaveBeenCalled()
  })

  it('deletes the selection CAPTURED when the confirm opened, one at a time, then leaves select mode', async () => {
    const w = mountView()
    await settle()
    await enterSelect(w)
    await tile(w, 'a').trigger('click')
    await tile(w, 'b').trigger('click')
    await dockBar()!.find('[data-test="selection-delete"]').trigger('click')
    expect(lastConfirm().header).toBe('Delete 2 items?')
    // A toggle while the dialog is up does not change what it deletes.
    await tile(w, 'b').trigger('click')
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    listed = [row('f', false)]
    await lastConfirm().accept()
    await settle()
    expect(deletes()).toEqual(['DELETE /api/attachments/a', 'DELETE /api/attachments/b'])
    expect(invalidate.mock.calls.map(([f]) => (f as { queryKey: unknown }).queryKey)).toEqual([
      ['attachments'],
      ['memos'],
    ])
    expect(registration.override.value).toBeNull()
    expect(w.find('[data-test="tile-select"]').exists()).toBe(false)
  })

  it('a second delete tap while the confirm is up opens no second confirm', async () => {
    const w = mountView()
    await settle()
    await enterSelect(w)
    await tile(w, 'a').trigger('click')
    const bar = dockBar()!
    await bar.find('[data-test="selection-delete"]').trigger('click')
    await bar.find('[data-test="selection-delete"]').trigger('click')
    expect(confirmRequire).toHaveBeenCalledTimes(1)
  })

  it('a refetch that drops a selected item drops it from the count', async () => {
    const w = mountView()
    await settle()
    await enterSelect(w)
    await tile(w, 'a').trigger('click')
    await tile(w, 'b').trigger('click')
    listed = [row('b'), row('f', false)]
    await queryClient.invalidateQueries({ queryKey: ['attachments'] })
    await settle()
    expect(dockBar()!.find('[data-test="selection-count"]').text()).toBe('1 Item Selected')
  })

  it('shares the originals of the selection, and offers no share where files cannot be shared', async () => {
    const share = vi.fn(async () => {})
    Object.defineProperty(navigator, 'canShare', { value: () => true, configurable: true })
    Object.defineProperty(navigator, 'share', { value: share, configurable: true })
    const fetchMock = vi.fn(async () => new Response(new Blob(['x'], { type: 'image/jpeg' })))
    vi.stubGlobal('fetch', fetchMock)
    const w = mountView()
    await settle()
    await enterSelect(w)
    await tile(w, 'b').trigger('click')
    await dockBar()!.find('[data-test="selection-share"]').trigger('click')
    await settle()
    expect(fetchMock.mock.calls.map((c) => (c as unknown as [string])[0])).toEqual(['/api/attachments/b/original'])
    const { files } = (share.mock.calls[0] as unknown as [ShareData])[0]
    expect(files!.map((f) => f.name)).toEqual(['b.jpg'])

    w.unmount()
    Object.defineProperty(navigator, 'canShare', { value: () => false, configurable: true })
    const w2 = mountView()
    await settle()
    await enterSelect(w2)
    expect(dockBar()!.find('[data-test="selection-share"]').exists()).toBe(false)
  })
})
