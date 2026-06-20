import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { describe, expect, it, vi } from 'vitest'

import HomeView from '@/views/HomeView.vue'

// CaptureEditor (rendered by Home) depends on the API; mock it (not the view under test).
vi.mock('@/composables/useApi', () => ({
  useApi: () => ({ GET: vi.fn(), POST: vi.fn() }),
}))

const TimelineStub = { template: '<div data-test="timeline-stub" />' }

describe('HomeView', () => {
  it('composes the capture editor above the timeline', () => {
    const wrapper = mount(HomeView, {
      global: { plugins: [PrimeVue], stubs: { MemoTimeline: TimelineStub } },
    })
    expect(wrapper.find('[data-test="capture-editor"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="timeline-stub"]').exists()).toBe(true)
  })
})
