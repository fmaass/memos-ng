import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import MemoFeed from '@/components/MemoFeed.vue'

function memoStub(uid: string, content: string, created_at = '2026-06-20T10:00:00Z') {
  return {
    uid,
    owner: 'alice',
    mine: true,
    content,
    visibility: 'PRIVATE' as const,
    pinned: false,
    created_at,
    updated_at: created_at,
    archived_at: null,
    tags: [],
    attachments: [],
    reactions: [],
    total_size: 0,
  }
}

const patch = vi.fn().mockResolvedValue({ error: undefined })
vi.mock('@/composables/useApi', () => ({ useApi: () => ({ PATCH: patch }) }))

const invalidateQueries = vi.fn()
vi.mock('@tanstack/vue-query', () => ({
  useQueryClient: () => ({ invalidateQueries }),
  useMutation: (opts: {
    mutationFn: (v: unknown) => Promise<unknown>
    onSuccess?: () => void
  }) => ({
    mutate: async (v: unknown) => {
      await opts.mutationFn(v)
      opts.onSuccess?.()
    },
  }),
}))

const fetchNextPage = vi.fn()
const refetch = vi.fn()
const useTimelineMock = vi.fn()
vi.mock('@/composables/useTimeline', () => ({ useTimeline: (q: unknown) => useTimelineMock(q) }))

function feedResult(items: ReturnType<typeof memoStub>[]) {
  return {
    data: ref({ pages: [{ items, nextCursor: null, totalCount: items.length }] }),
    fetchNextPage,
    refetch,
    hasNextPage: ref(false),
    isFetchingNextPage: ref(false),
    isFetching: ref(false),
    isPending: ref(false),
    isError: ref(false),
  }
}

function mountFeed(props = {}) {
  return mount(MemoFeed, { props, global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
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
    patch.mockClear()
    invalidateQueries.mockClear()
    refetch.mockClear()
  })

  it('renders a card per memo under day-grouped headers', () => {
    useTimelineMock.mockReturnValue(
      feedResult([
        memoStub('1', '**first**', '2026-06-20T10:00:00Z'),
        memoStub('2', 'older', '2026-01-01T10:00:00Z'),
      ]),
    )
    const wrapper = mountFeed()
    expect(wrapper.findAll('[data-test="memo-card"]')).toHaveLength(2)
    expect(wrapper.findAll('[data-test="day-header"]').length).toBe(2) // two distinct days
  })

  it('shows an empty state when there are no memos', () => {
    useTimelineMock.mockReturnValue(feedResult([]))
    expect(mountFeed().find('[data-test="feed-empty"]').exists()).toBe(true)
  })

  it('shows shimmer skeletons while the timeline is pending (not a blank screen)', () => {
    useTimelineMock.mockReturnValue({ ...feedResult([]), isPending: ref(true) })
    const wrapper = mountFeed()
    expect(wrapper.findAll('[data-test="memo-skeleton"]').length).toBeGreaterThan(0)
    expect(wrapper.find('[data-test="feed-empty"]').exists()).toBe(false) // skeletons, not empty
  })

  it('passes its query through to useTimeline', () => {
    useTimelineMock.mockReturnValue(feedResult([]))
    mountFeed({ query: { visibility: 'PUBLIC' } })
    const arg = useTimelineMock.mock.calls[0][0]
    expect(typeof arg === 'function' ? arg() : arg).toEqual({ visibility: 'PUBLIC' })
  })

  it('persists a task toggle bubbled from a card via PATCH', async () => {
    useTimelineMock.mockReturnValue(feedResult([memoStub('1', '- [ ] todo')]))
    const wrapper = mountFeed()
    wrapper.findComponent({ name: 'MemoCard' }).vm.$emit('task-toggle', {
      uid: '1',
      content: '- [x] todo',
    })
    await flushPromises()
    expect(patch).toHaveBeenCalledWith('/api/memos/{uid}', {
      params: { path: { uid: '1' } },
      body: { content: '- [x] todo' },
    })
    expect(invalidateQueries).toHaveBeenCalled()
  })

  it('re-mints the feed when a card bubbles retry, throttled to one refetch per cooldown', async () => {
    useTimelineMock.mockReturnValue(feedResult([memoStub('1', 'pic')]))
    const wrapper = mountFeed()
    const card = wrapper.findComponent({ name: 'MemoCard' })
    card.vm.$emit('retry', 'a1') // a tile exhausted its in-place retries (expired signed URL)
    await flushPromises()
    expect(refetch).toHaveBeenCalledTimes(1) // refetch re-mints freshly-signed URLs
    card.vm.$emit('retry', 'a1') // another tile fails immediately after
    await flushPromises()
    expect(refetch).toHaveBeenCalledTimes(1) // cooldown blocks a refetch loop
  })
})
