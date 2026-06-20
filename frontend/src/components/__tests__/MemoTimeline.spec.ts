import { mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import MemoTimeline from '@/components/MemoTimeline.vue'

function memoStub(uid: string, content: string) {
  return {
    uid,
    owner: 'alice',
    content,
    visibility: 'PRIVATE' as const,
    pinned: false,
    created_at: '2026-06-20T00:00:00Z',
    updated_at: '2026-06-20T00:00:00Z',
    archived_at: null,
    tags: [],
    total_size: 0,
  }
}

const fetchNextPage = vi.fn()
vi.mock('@/composables/useTimeline', () => ({
  useTimeline: () => ({
    data: ref({
      pages: [{ items: [memoStub('1', '**first**'), memoStub('2', 'second')], nextCursor: null, totalCount: 2 }],
    }),
    fetchNextPage,
    hasNextPage: ref(false),
    isFetchingNextPage: ref(false),
    isPending: ref(false),
    isError: ref(false),
  }),
}))

describe('Timeline', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        observe() {}
        disconnect() {}
        unobserve() {}
      },
    )
  })
  afterEach(() => vi.unstubAllGlobals())

  it('renders a card per memo across the loaded pages', () => {
    const wrapper = mount(MemoTimeline)
    const cards = wrapper.findAll('[data-test="memo-card"]')
    expect(cards).toHaveLength(2)
    expect(wrapper.find('[data-test="memo-content"]').html()).toContain('<strong>first</strong>')
  })
})
