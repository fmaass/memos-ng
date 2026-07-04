import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

// The view seeds its filter from the route and syncs back via the router; drive it through the
// URL (the real wiring) rather than PrimeVue widgets in happy-dom. FilterBar's tag list uses
// useTags — stub it (a dependency, not the unit under test).
const replace = vi.fn()
const routeQuery: { value: Record<string, string | string[]> } = { value: {} }
vi.mock('vue-router', () => ({
  useRoute: () => ({
    get query() {
      return routeQuery.value
    },
  }),
  useRouter: () => ({ replace }),
}))
vi.mock('@/composables/useTags', () => ({ useTags: () => ({ data: ref([]) }) }))

import SearchView from '@/views/SearchView.vue'

const FeedStub = {
  props: ['query'],
  template: '<div data-test="feed-stub">{{ JSON.stringify(query) }}</div>',
}

function mountSearch() {
  return mount(SearchView, {
    global: {
      plugins: [PrimeVue, ConfirmationService, ToastService],
      stubs: { MemoFeed: FeedStub },
    },
  })
}

describe('SearchView', () => {
  it('shows a hint and no feed when the URL carries no filter', () => {
    routeQuery.value = {}
    const wrapper = mountSearch()
    expect(wrapper.find('[data-test="search-hint"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="feed-stub"]').exists()).toBe(false)
  })

  it('renders the feed with the filter parsed from the URL query', () => {
    routeQuery.value = { q: 'beach', tag: ['work'] }
    const wrapper = mountSearch()
    const feed = wrapper.find('[data-test="feed-stub"]')
    expect(feed.exists()).toBe(true)
    expect(feed.text()).toContain('"q":"beach"')
    expect(feed.text()).toContain('"tag":["work"]')
  })
})
