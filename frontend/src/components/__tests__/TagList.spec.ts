import { mount } from '@vue/test-utils'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import TagList from '@/components/TagList.vue'

vi.mock('@/composables/useTags', () => ({
  useTags: () => ({ data: ref([{ name: 'work', count: 3 }, { name: 'idea', count: 1 }]) }),
}))

const RouterLinkStub = { template: '<a><slot /></a>' }

describe('TagList', () => {
  it('renders each tag with its memo count', () => {
    const wrapper = mount(TagList, { global: { stubs: { RouterLink: RouterLinkStub } } })
    const items = wrapper.findAll('[data-test="tag-item"]')
    expect(items).toHaveLength(2)
    expect(items[0].text()).toContain('#work')
    expect(items[0].text()).toContain('3')
  })
})
