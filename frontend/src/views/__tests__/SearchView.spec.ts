import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

// The view seeds its filter from the route AND re-seeds on query-only navigation, syncing back via
// the router; drive it through the URL (the real wiring) rather than PrimeVue widgets in happy-dom.
// A reactive ref backs route.query so a query change actually fires the view's route watcher.
const { replace, routeQuery } = vi.hoisted(() => {
  return { replace: vi.fn(), routeQuery: { current: null as unknown as ReturnType<typeof ref> } }
})
vi.mock('vue-router', async () => {
  const { ref } = await import('vue')
  routeQuery.current = ref<Record<string, string | string[]>>({})
  return {
    useRoute: () => ({
      get query() {
        return routeQuery.current.value
      },
    }),
    useRouter: () => ({ replace }),
  }
})
vi.mock('@/composables/useTags', () => ({ useTags: () => ({ data: ref([]) }) }))
function setQuery(q: Record<string, string | string[]>) {
  routeQuery.current.value = q
}

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
    setQuery({})
    const wrapper = mountSearch()
    expect(wrapper.find('[data-test="search-hint"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="feed-stub"]').exists()).toBe(false)
  })

  it('renders the feed with the filter parsed from the URL query', () => {
    setQuery({ q: 'beach', tag: ['work'] })
    const wrapper = mountSearch()
    const feed = wrapper.find('[data-test="feed-stub"]')
    expect(feed.exists()).toBe(true)
    expect(feed.text()).toContain('"q":"beach"')
    expect(feed.text()).toContain('"tag":["work"]')
  })

  it('re-seeds the feed on query-only navigation (no full remount)', async () => {
    setQuery({ q: 'beach' })
    const wrapper = mountSearch()
    expect(wrapper.find('[data-test="feed-stub"]').text()).toContain('"q":"beach"')
    // simulate a sidebar tag link / back-forward that only changes the query while mounted
    setQuery({ tag: ['work'] })
    await wrapper.vm.$nextTick()
    const feed = wrapper.find('[data-test="feed-stub"]')
    expect(feed.text()).toContain('"tag":["work"]')
    expect(feed.text()).not.toContain('beach')
  })
})
