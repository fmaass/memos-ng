import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import ResourcesView from '@/views/ResourcesView.vue'

vi.mock('@/composables/useApi', () => ({ useApi: () => ({ GET: vi.fn() }) }))
vi.mock('@tanstack/vue-query', () => ({
  useQuery: () => ({
    data: ref([
      {
        uid: 'a1',
        mime: 'image/jpeg',
        status: 'ready',
        blurhash: null,
        orphan: true,
        thumbnailUrl: '/api/attachments/a1/thumb',
        previewUrl: '/api/attachments/a1/mobile',
        webUrl: '/api/attachments/a1/web',
        posterUrl: '/api/attachments/a1/poster',
        downloadUrl: '/api/attachments/a1/original',
      },
    ]),
    isPending: ref(false),
  }),
}))

describe('ResourcesView', () => {
  it('renders a media tile per attachment and flags orphans', () => {
    const wrapper = mount(ResourcesView, { global: { plugins: [PrimeVue] } })
    expect(wrapper.findAll('[data-test="media-tile"]')).toHaveLength(1)
    expect(wrapper.find('[data-test="orphan-badge"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="type-filter"]').exists()).toBe(true)
  })
})
