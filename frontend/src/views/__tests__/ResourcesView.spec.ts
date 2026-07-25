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
vi.mock('@/composables/useMediaLightbox', () => ({
  useMediaLightbox: () => ({ open: vi.fn(), destroy: vi.fn() }),
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

  it('re-mints Resources when gallery retry bubbles, throttled to one refetch per cooldown', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(30_000)
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    wrapper.findComponent({ name: 'JustifiedGallery' }).vm.$emit('retry', 'a1')
    expect(refetch).toHaveBeenCalledTimes(1)
    wrapper.findComponent({ name: 'JustifiedGallery' }).vm.$emit('retry', 'a1')
    expect(refetch).toHaveBeenCalledTimes(1)
  })
})
