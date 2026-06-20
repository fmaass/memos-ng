import { VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { describe, expect, it, vi } from 'vitest'

import HomeView from '@/views/HomeView.vue'

// Mock the API dependency (not the component under test). The mocked /health
// response must flow through useQuery into the rendered Tag for this to pass.
vi.mock('@/composables/useApi', () => ({
  useApi: () => ({
    GET: vi.fn().mockResolvedValue({ data: { status: 'ok', version: 'test' }, error: undefined }),
  }),
}))

describe('HomeView', () => {
  it('renders backend health status reported by the API', async () => {
    const wrapper = mount(HomeView, {
      global: { plugins: [VueQueryPlugin, PrimeVue] },
    })
    await flushPromises()
    expect(wrapper.find('[data-test="health"]').text()).toContain('ok')
  })
})
