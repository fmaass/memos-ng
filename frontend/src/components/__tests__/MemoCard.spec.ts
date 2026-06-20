import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import MemoCard from '@/components/MemoCard.vue'

function memo(content: string, tags: string[] = []) {
  return {
    uid: 'm1',
    owner: 'alice',
    content,
    visibility: 'PRIVATE' as const,
    pinned: false,
    created_at: '2026-06-20T00:00:00Z',
    updated_at: '2026-06-20T00:00:00Z',
    archived_at: null,
    tags,
    attachments: [],
    total_size: 0,
  }
}

describe('MemoCard', () => {
  it('renders markdown content and tag chips', () => {
    const wrapper = mount(MemoCard, { props: { memo: memo('**bold** memo', ['work', 'idea']) } })
    expect(wrapper.find('[data-test="memo-content"]').html()).toContain('<strong>bold</strong>')
    expect(wrapper.findAll('[data-test="tag-chip"]').map((c) => c.text())).toEqual(['#work', '#idea'])
  })

  it('renders a <script> in memo content inert (no live script tag)', () => {
    const wrapper = mount(MemoCard, { props: { memo: memo('evil <script>alert(1)</script>') } })
    const content = wrapper.find('[data-test="memo-content"]')
    expect(content.element.querySelector('script')).toBeNull()
    expect(content.html()).toContain('&lt;script&gt;')
  })

  it('has no restore action unless restorable', () => {
    const wrapper = mount(MemoCard, { props: { memo: memo('x') } })
    expect(wrapper.find('[data-test="restore"]').exists()).toBe(false)
  })

  it('emits restore with the uid when restorable', async () => {
    const wrapper = mount(MemoCard, { props: { memo: memo('x'), restorable: true } })
    await wrapper.find('[data-test="restore"]').trigger('click')
    expect(wrapper.emitted('restore')?.[0]).toEqual(['m1'])
  })
})
