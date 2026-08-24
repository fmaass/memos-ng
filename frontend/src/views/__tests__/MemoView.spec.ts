import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ToastService from 'primevue/toastservice'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import MemoView from '@/views/MemoView.vue'

const route = { params: { uid: 'u123' } as Record<string, string> }
vi.mock('vue-router', () => ({ useRoute: () => route }))

const GET = vi.fn()
const PATCH = vi.fn().mockResolvedValue({ error: undefined })
const POST = vi.fn().mockResolvedValue({ error: undefined })
const DELETE = vi.fn().mockResolvedValue({ error: undefined })
vi.mock('@/composables/useApi', () => ({ useApi: () => ({ GET, PATCH, POST, DELETE }) }))

vi.mock('@/composables/usePrintMaster', () => ({
  usePrintMaster: () => ({ attach: vi.fn(), remove: vi.fn(), retry: vi.fn(), uploads: new Map() }),
  fetchPrintMasters: vi.fn().mockResolvedValue([]),
  printMasterUploads: () => new Map(),
}))

const invalidateQueries = vi.fn()
const refetch = vi.fn()
const queryKeys: unknown[] = []
// useQuery runs the view's REAL queryFn against the mocked client, so the fetch and its 404
// branch are what these tests exercise — a stubbed query result would assert nothing about them.
vi.mock('@tanstack/vue-query', async () => {
  const { ref, toValue } = await import('vue')
  return {
    useQueryClient: () => ({ invalidateQueries }),
    useQuery: (opts: { queryKey: unknown; queryFn: () => Promise<unknown> }) => {
      queryKeys.push(toValue(opts.queryKey))
      const data = ref<unknown>(undefined)
      const isPending = ref(true)
      const isError = ref(false)
      void opts
        .queryFn()
        .then((d) => {
          data.value = d
        })
        .catch(() => {
          isError.value = true
        })
        .finally(() => {
          isPending.value = false
        })
      return { data, isPending, isError, refetch }
    },
    useMutation: (opts: { mutationFn: (v: never) => Promise<unknown>; onSuccess?: () => void }) => ({
      mutate: async (v: never) => {
        await opts.mutationFn(v)
        opts.onSuccess?.()
      },
    }),
  }
})

function memoStub(uid = 'u123', content = 'a shared memo') {
  return {
    uid,
    owner: 'alice',
    mine: true,
    content,
    visibility: 'PRIVATE' as const,
    pinned: false,
    created_at: '2026-06-20T10:00:00Z',
    updated_at: '2026-06-20T10:00:00Z',
    archived_at: null,
    tags: [],
    attachments: [],
    reactions: [],
    total_size: 0,
  }
}

const CardStub = {
  props: ['memo'],
  emits: ['task-toggle', 'delete-memo'],
  template: `<div data-test="card">
    <span data-test="card-content">{{ memo.content }}</span>
    <button data-test="do-task" @click="$emit('task-toggle', { uid: memo.uid, content: '- [x] x' })" />
    <button data-test="do-delete-memo" @click="$emit('delete-memo', memo.uid)" />
  </div>`,
}

function mountView() {
  return mount(MemoView, {
    global: {
      plugins: [PrimeVue, ToastService],
      stubs: { MemoCard: CardStub, MemoCardSkeleton: true },
    },
  })
}

function apiResult(status: number, body: unknown) {
  return status === 200
    ? { data: body, error: undefined, response: { status } }
    : { data: undefined, error: body, response: { status } }
}

describe('MemoView (/m/:uid)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    queryKeys.length = 0
    route.params = { uid: 'u123' }
    GET.mockResolvedValue(apiResult(200, memoStub()))
  })

  it('fetches the memo named by the route and renders it in the shared memo card', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(GET).toHaveBeenCalledWith('/api/memos/{uid}', { params: { path: { uid: 'u123' } } })
    expect(wrapper.find('[data-test="card-content"]').text()).toBe('a shared memo')
    expect(wrapper.find('[data-test="memo-missing"]').exists()).toBe(false)
  })

  it('follows the uid in the URL, not a fixed one', async () => {
    route.params = { uid: 'other-uid' }
    mountView()
    await flushPromises()

    expect(GET).toHaveBeenCalledWith('/api/memos/{uid}', { params: { path: { uid: 'other-uid' } } })
  })

  it('keys its cache OUTSIDE the ["memos"] prefix', async () => {
    // Regression guard: useMediaEvents walks every cache entry under ['memos'] and reads
    // `data.pages` off it (hasInFlight). A single-memo entry parked under that prefix throws
    // there and kills the live media channel app-wide.
    mountView()
    await flushPromises()

    expect(queryKeys[0]).toEqual(['memo', 'u123'])
    expect((queryKeys[0] as string[])[0]).not.toBe('memos')
  })

  it('states that an unknown uid is not here, instead of erroring', async () => {
    GET.mockResolvedValue(apiResult(404, { detail: 'Memo not found' }))
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-test="memo-missing"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="card"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="memo-error"]').exists()).toBe(false)
  })

  it('keeps a real failure distinct from a missing memo', async () => {
    GET.mockResolvedValue(apiResult(500, { detail: 'boom' }))
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-test="memo-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="memo-missing"]').exists()).toBe(false)
  })

  it('shows the skeleton until the memo arrives', () => {
    const wrapper = mountView()
    expect(wrapper.find('memo-card-skeleton-stub').exists()).toBe(true)
    expect(wrapper.find('[data-test="card"]').exists()).toBe(false)
  })

  it('persists a task toggle and refreshes this memo AND the feeds behind it', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-test="do-task"]').trigger('click')
    await flushPromises()

    expect(PATCH).toHaveBeenCalledWith('/api/memos/{uid}', {
      params: { path: { uid: 'u123' } },
      body: { content: '- [x] x' },
    })
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['memo', 'u123'] })
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['memos'] })
  })

  it('deletes the memo it is showing', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-test="do-delete-memo"]').trigger('click')
    await flushPromises()

    expect(DELETE).toHaveBeenCalledWith('/api/memos/{uid}', { params: { path: { uid: 'u123' } } })
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['memo', 'u123'] })
  })

  it('reads in the shared content column (wider on xl desktops)', () => {
    const classes = mountView().find('[data-test="view-memo"]').classes()
    expect(classes).toContain('max-w-2xl')
    expect(classes).toContain('xl:max-w-4xl')
  })
})
