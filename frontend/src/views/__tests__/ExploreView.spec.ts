import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ExploreView from '@/views/ExploreView.vue'

const FeedStub = {
  props: ['query'],
  template: '<div data-test="feed-stub">{{ JSON.stringify(query) }}</div>',
}

describe('ExploreView', () => {
  it('renders the feed filtered to public memos', () => {
    const wrapper = mount(ExploreView, { global: { stubs: { MemoFeed: FeedStub } } })
    expect(wrapper.find('[data-test="view-explore"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="feed-stub"]').text()).toContain('"visibility":"PUBLIC"')
  })
})
