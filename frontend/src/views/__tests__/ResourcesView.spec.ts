import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ResourcesView from '@/views/ResourcesView.vue'

const refetch = vi.fn()

vi.mock('@/composables/useApi', () => ({ useApi: () => ({ GET: vi.fn(), DELETE: vi.fn() }) }))
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
vi.mock('@tanstack/vue-query', () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useQuery: () => ({
    data: ref([
      {
        uid: 'a1',
        mime: 'image/jpeg',
        status: 'ready',
        blurhash: null,
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
      },
    ]),
    isPending: ref(false),
    isFetching: ref(false),
    refetch,
  }),
}))

describe('ResourcesView', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    refetch.mockClear()
    lightboxOpen.mockClear()
    printAttach.mockClear()
    printRemove.mockClear()
    printRetry.mockClear()
    usePrintMasterMock.mockClear()
  })

  it('renders the justified gallery + type filter (not the old square grid)', () => {
    // Layout/image sizing needs real DOM width (covered by lib/justified.spec); here we assert the
    // gallery container + filter are wired and the legacy grid/tile markup is gone.
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    expect(wrapper.find('[data-test="justified-gallery"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="type-filter"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="resource-grid"]').exists()).toBe(false)
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
