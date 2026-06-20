import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { describe, expect, it, vi } from 'vitest'

import HomeView from '@/views/HomeView.vue'

// CaptureEditor (rendered by Home) depends on the API; mock it (not the view under test).
vi.mock('@/composables/useApi', () => ({
  useApi: () => ({ GET: vi.fn(), POST: vi.fn() }),
}))

describe('HomeView', () => {
  it('renders the capture editor atop the home timeline', () => {
    const wrapper = mount(HomeView, { global: { plugins: [PrimeVue] } })
    expect(wrapper.find('[data-test="capture-editor"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="timeline-placeholder"]').exists()).toBe(true)
  })
})
