import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { describe, expect, it, vi } from 'vitest'

import HomeView from '@/views/HomeView.vue'

// CaptureEditor (rendered by Home) depends on the API; mock it (not the view under test).
vi.mock('@/composables/useApi', () => ({
  useApi: () => ({ GET: vi.fn(), POST: vi.fn() }),
}))
// useMediaEvents opens an SSE channel + uses the query client; stub it out here.
vi.mock('@/composables/useMediaEvents', () => ({ useMediaEvents: () => {} }))
// HomeView reads the route's ?tag; provide a stub route.
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))
// CaptureEditor (child) uses settings via vue-query; stub it out.
vi.mock('@/composables/useSettings', async () => {
  const { ref } = await import('vue')
  return { useSettings: () => ({ settings: ref(undefined), update: { mutate: () => {} } }) }
})

const FeedStub = { template: '<div data-test="feed-stub" />' }

describe('HomeView', () => {
  it('composes the capture editor above the memo feed', () => {
    const wrapper = mount(HomeView, {
      global: { plugins: [PrimeVue, ConfirmationService, ToastService], stubs: { MemoFeed: FeedStub } },
    })
    expect(wrapper.find('[data-test="capture-editor"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="feed-stub"]').exists()).toBe(true)
  })
})
