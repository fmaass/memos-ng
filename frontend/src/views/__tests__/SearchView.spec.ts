import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import SearchView from '@/views/SearchView.vue'

const FeedStub = {
  props: ['query'],
  template: '<div data-test="feed-stub">{{ JSON.stringify(query) }}</div>',
}

function mountSearch() {
  return mount(SearchView, { global: { plugins: [PrimeVue, ConfirmationService, ToastService], stubs: { MemoFeed: FeedStub } } })
}

describe('SearchView', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('shows a hint and no feed before a search term', () => {
    const wrapper = mountSearch()
    expect(wrapper.find('[data-test="search-hint"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="feed-stub"]').exists()).toBe(false)
  })

  it('debounces input then queries the feed with q', async () => {
    const wrapper = mountSearch()
    const input = wrapper.find('[data-test="search-input"]')
    await input.setValue('beach')
    await input.trigger('input')
    expect(wrapper.find('[data-test="feed-stub"]').exists()).toBe(false) // not yet (debounced)

    vi.advanceTimersByTime(300)
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-test="feed-stub"]').text()).toContain('"q":"beach"')
  })
})
