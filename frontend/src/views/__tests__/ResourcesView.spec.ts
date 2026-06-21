import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import ResourcesView from '@/views/ResourcesView.vue'

vi.mock('@/composables/useApi', () => ({ useApi: () => ({ GET: vi.fn(), DELETE: vi.fn() }) }))
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
  }),
}))

describe('ResourcesView', () => {
  it('renders the justified gallery + type filter (not the old square grid)', () => {
    // Layout/image sizing needs real DOM width (covered by lib/justified.spec); here we assert the
    // gallery container + filter are wired and the legacy grid/tile markup is gone.
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
    expect(wrapper.find('[data-test="justified-gallery"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="type-filter"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="resource-grid"]').exists()).toBe(false)
  })
})
