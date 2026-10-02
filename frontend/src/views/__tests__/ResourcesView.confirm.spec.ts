import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ConfirmDialog from 'primevue/confirmdialog'
import ToastService from 'primevue/toastservice'
import { defineComponent, h, nextTick } from 'vue'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { createDockSegment, DOCK_SEGMENT_KEY, type DockSegmentRegistration } from '@/composables/useDockSegment'
import ResourcesView from '@/views/ResourcesView.vue'

// The bulk-delete confirm as the app runs it: the REAL ConfirmationService and <ConfirmDialog /> (not
// the mocked useConfirm of ResourcesView.select.spec), and the dock toolbar rendered reactively in the
// same tree, so PrimeVue's own focus capture/restore and its document-level Escape listener run
// against the real toolbar button. Only the transport under the generated client is scripted.
const { transport } = vi.hoisted(() => ({ transport: vi.fn<(req: Request) => Promise<Response>>() }))
vi.mock('@/composables/useApi', async () => {
  const { default: createClient } = await import('openapi-fetch')
  const client = createClient({ baseUrl: '/', fetch: (req: Request) => transport(req) })
  return { useApi: () => client }
})
vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const live = reactive({ query: {} })
  return { useRouter: () => ({ push: vi.fn(), replace: vi.fn() }), useRoute: () => live }
})
vi.mock('@/composables/useMediaLightbox', () => ({
  useMediaLightbox: () => ({ open: vi.fn(), destroy: vi.fn(), refreshPrintMaster: vi.fn() }),
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
const ok = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
const deletes = () => transport.mock.calls.filter(([req]) => req.method === 'DELETE')

let wrapper: VueWrapper | null = null
let registration: DockSegmentRegistration
function mountApp() {
  registration = createDockSegment()
  // The shell in miniature: the view, the one global confirm, and the dock rendering the override.
  const Shell = defineComponent({
    setup: () => () =>
      h('div', [
        h(ResourcesView),
        h(ConfirmDialog),
        registration.override.value ? h('nav', [h(registration.override.value)]) : null,
      ]),
  })
  wrapper = mount(Shell, {
    attachTo: document.body,
    global: {
      plugins: [
        [VueQueryPlugin, { queryClient: new QueryClient({ defaultOptions: { queries: { retry: false } } }) }],
        PrimeVue,
        ToastService,
        ConfirmationService,
      ],
      provide: { [DOCK_SEGMENT_KEY as symbol]: registration },
      // PrimeVue's Dialog binds its Escape listener and captures/restores focus in its transition
      // hooks, which the default Transition stub never calls.
      stubs: { transition: false },
    },
  })
  return wrapper
}
async function settle() {
  for (let i = 0; i < 10; i++) await flushPromises()
}

const dialog = () => document.querySelector<HTMLElement>('[role="alertdialog"]')
const dialogButton = (label: string) =>
  [...(dialog()?.querySelectorAll<HTMLButtonElement>('button') ?? [])].find((b) => b.textContent?.trim() === label) ??
  null
const toolbarDelete = () => document.querySelector<HTMLButtonElement>('[data-test="selection-delete"]')
const selecting = () => document.querySelector('[data-test="tile-select"]') !== null
const pressed = (uid: string) =>
  document.querySelector(`[data-uid="${uid}"] [data-test="tile-select"]`)?.getAttribute('aria-pressed')
function escapeFrom(el: Element) {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }))
}

// Select tile a, then open the confirm from the toolbar's delete button the way a pointer would:
// the button holds focus when it is activated, so it is what PrimeVue captures to restore later.
async function openConfirm() {
  const w = mountApp()
  await settle()
  await w.find('[data-test="library-select"]').trigger('click')
  await nextTick()
  ;(document.querySelector('[data-uid="a"] [data-test="tile-select"]') as HTMLElement).click()
  await nextTick()
  const del = toolbarDelete()!
  del.focus()
  expect(document.activeElement).toBe(del)
  del.click()
  // The Dialog places its initial focus in its after-enter hook, which runs as the enter transition
  // drops its classes; the focus trap may park focus elsewhere in the dialog before that.
  await vi.waitFor(() => {
    expect(dialog()).not.toBeNull()
    expect(dialog()!.classList.contains('p-dialog-enter-active')).toBe(false)
  })
  expect(dialog()!.contains(document.activeElement)).toBe(true)
  return del
}

// happy-dom 20 gives buttons no `autofocus` IDL attribute (only input/select/textarea have one), so
// Vue writes PrimeVue's `autofocus: false` as the literal attribute autofocus="false" — which a browser
// never renders and which the Dialog's `[autofocus]` lookup then matches, handing Cancel the focus
// whatever the option says. Model the reflected boolean the HTML spec defines on every element, as
// Chrome has it, so the dialog chooses its initial focus the way the browser does.
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, 'autofocus', {
    configurable: true,
    get(this: HTMLElement) {
      return this.hasAttribute('autofocus')
    },
    set(this: HTMLElement, on: boolean) {
      this.toggleAttribute('autofocus', Boolean(on))
    },
  })
})
afterAll(() => {
  Reflect.deleteProperty(HTMLElement.prototype, 'autofocus')
})

describe('ResourcesView bulk-delete confirm focus', () => {
  beforeEach(() => {
    transport.mockReset()
    transport.mockImplementation(async (req) => {
      if (req.method === 'DELETE') return new Response(null, { status: 204 })
      return ok(new URL(req.url).pathname === '/api/attachments' ? [row('a'), row('b')] : [])
    })
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

  it('opens with focus on Cancel, and activating the focused control deletes nothing', async () => {
    await openConfirm()
    const cancel = dialogButton('Cancel')
    expect(cancel).not.toBeNull()
    expect(document.activeElement).toBe(cancel)
    expect(document.activeElement).not.toBe(dialogButton('Delete'))
    // happy-dom never turns Enter on a focused button into a click (HTMLButtonElement.ts only
    // activates on a dispatched click), so "Enter" is modelled as activating the focused element.
    ;(document.activeElement as HTMLElement).click()
    await vi.waitFor(() => expect(dialog()).toBeNull())
    await settle()
    expect(deletes()).toHaveLength(0)
    expect(selecting()).toBe(true)
    expect(pressed('a')).toBe('true')
  })

  it('Cancel closes the confirm, keeps select mode, and returns focus to the toolbar delete button', async () => {
    const del = await openConfirm()
    dialogButton('Cancel')!.click()
    await vi.waitFor(() => expect(dialog()).toBeNull())
    expect(selecting()).toBe(true)
    expect(pressed('a')).toBe('true')
    expect(toolbarDelete()).toBe(del)
    await vi.waitFor(() => expect(document.activeElement).toBe(del))
    expect(deletes()).toHaveLength(0)
  })

  it('Escape closes only the confirm: select mode, the selection and the toolbar survive, focus returns to delete', async () => {
    const del = await openConfirm()
    escapeFrom(document.activeElement!)
    await vi.waitFor(() => expect(dialog()).toBeNull())
    expect(selecting()).toBe(true)
    expect(pressed('a')).toBe('true')
    expect(toolbarDelete()).toBe(del)
    await vi.waitFor(() => expect(document.activeElement).toBe(del))
    expect(deletes()).toHaveLength(0)
  })

  it('a second Escape, with the confirm gone, leaves select mode', async () => {
    await openConfirm()
    escapeFrom(document.activeElement!)
    await vi.waitFor(() => expect(dialog()).toBeNull())
    await vi.waitFor(() => expect(document.activeElement).toBe(toolbarDelete()))
    expect(selecting()).toBe(true)
    escapeFrom(document.activeElement!)
    await nextTick()
    expect(selecting()).toBe(false)
    expect(toolbarDelete()).toBeNull()
  })
})
