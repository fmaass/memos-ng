import { mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import MemoFeed from '@/components/MemoFeed.vue'

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
    attachments: [],
    total_size: 0,
  }
}

const fetchNextPage = vi.fn()
const useTimelineMock = vi.fn()
vi.mock('@/composables/useTimeline', () => ({ useTimeline: (q: unknown) => useTimelineMock(q) }))

function feedResult(items: ReturnType<typeof memoStub>[]) {
  return {
    data: ref({ pages: [{ items, nextCursor: null, totalCount: items.length }] }),
    fetchNextPage,
    hasNextPage: ref(false),
    isFetchingNextPage: ref(false),
    isPending: ref(false),
    isError: ref(false),
  }
}

describe('MemoFeed', () => {
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
  afterEach(() => {
    vi.unstubAllGlobals()
    useTimelineMock.mockReset()
  })

  it('renders a card per memo', () => {
    useTimelineMock.mockReturnValue(feedResult([memoStub('1', '**first**'), memoStub('2', 'second')]))
    const wrapper = mount(MemoFeed)
    expect(wrapper.findAll('[data-test="memo-card"]')).toHaveLength(2)
    expect(wrapper.find('[data-test="memo-content"]').html()).toContain('<strong>first</strong>')
  })

  it('shows an empty state when there are no memos', () => {
    useTimelineMock.mockReturnValue(feedResult([]))
    const wrapper = mount(MemoFeed)
    expect(wrapper.find('[data-test="feed-empty"]').exists()).toBe(true)
  })

  it('passes its query through to useTimeline', () => {
    useTimelineMock.mockReturnValue(feedResult([]))
    mount(MemoFeed, { props: { query: { visibility: 'PUBLIC' } } })
    const arg = useTimelineMock.mock.calls[0][0]
    expect(typeof arg === 'function' ? arg() : arg).toEqual({ visibility: 'PUBLIC' })
  })
})
