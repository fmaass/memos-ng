import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { nextTick, ref } from 'vue'
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
// The double carries `isPending` and `variables` because the real one does, and the feed reads
// them to tell a card that ITS edit is still in flight (MEMNG-262). A double missing them would
// make that wiring untestable — and silently break the component it is standing in for.
vi.mock('@tanstack/vue-query', async () => {
  const { ref } = await import('vue')
  return {
    useQueryClient: () => ({ invalidateQueries }),
    useMutation: (opts: {
      mutationFn: (v: unknown) => Promise<unknown>
      onMutate?: (v: unknown) => void
      onSuccess?: () => void
      onError?: (e: unknown) => void
      onSettled?: (data: unknown, error: unknown, v: unknown) => void
    }) => {
      // ONE observer, many calls — exactly like the real one, including the part that matters
      // here: `variables` describes the LATEST call, while onMutate/onSettled run per call.
      const isPending = ref(false)
      const variables = ref<unknown>(undefined)
      return {
        isPending,
        variables,
        mutate: (v: unknown) => {
          variables.value = v
          isPending.value = true
          opts.onMutate?.(v)
          return opts
            .mutationFn(v)
            .then((data) => {
              opts.onSuccess?.()
              opts.onSettled?.(data, null, v)
            })
            .catch((error: unknown) => {
              opts.onError?.(error)
              opts.onSettled?.(undefined, error, v)
            })
            .finally(() => {
              isPending.value = false
            })
        },
      }
    },
  }
})

// Print masters live in a shared composable (feed + Resources lightbox) and are unit-tested there;
// here we only pin that the feed wires those mutations, with its own cache key.
const { printAttach, printRemove, printRetry, usePrintMasterMock } = vi.hoisted(() => {
  const printAttach = vi.fn()
  const printRemove = vi.fn()
  const printRetry = vi.fn()
  return {
    printAttach,
    printRemove,
    printRetry,
    usePrintMasterMock: vi.fn(() => ({
      attach: printAttach,
      remove: printRemove,
      retry: printRetry,
      uploads: new Map(),
    })),
  }
})
vi.mock('@/composables/usePrintMaster', () => ({
  usePrintMaster: usePrintMasterMock,
  fetchPrintMasters: vi.fn().mockResolvedValue([]),
  printMasterUploads: () => new Map(),
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
    printAttach.mockClear()
    printRemove.mockClear()
    printRetry.mockClear()
    usePrintMasterMock.mockClear()
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

  it('routes the print-master verbs bubbled from a card to the shared mutations', async () => {
    // Before MEMNG-74 the feed carried its own copy with no error handling — a rejected upload
    // reported nothing here while the same action in Resources toasted.
    useTimelineMock.mockReturnValue(feedResult([memoStub('1', 'pic')]))
    const wrapper = mountFeed()
    const file = new File(['tiff-bytes'], 'master.tif', { type: 'image/tiff' })
    const card = wrapper.findComponent({ name: 'MemoCard' })

    card.vm.$emit('attach-print', { uid: 'a1', file, variantKey: 'print' })
    card.vm.$emit('remove-print', { uid: 'a1', variantKey: 'print:glossy' })
    card.vm.$emit('retry-print', 'a1')
    await flushPromises()

    expect(usePrintMasterMock).toHaveBeenCalledWith(['memos']) // the feed's own cache
    expect(printAttach).toHaveBeenCalledWith({ uid: 'a1', file, variantKey: 'print' })
    expect(printRemove).toHaveBeenCalledWith({ uid: 'a1', variantKey: 'print:glossy' })
    expect(printRetry).toHaveBeenCalledWith('a1')
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

describe('MemoFeed — one PATCH per memo, however many are in flight (MEMNG-262)', () => {
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
    patch.mockReset()
    patch.mockResolvedValue({ error: undefined })
  })

  /** Freshly located each time: a card re-renders, and a held wrapper goes stale. */
  const cardAt = (wrapper: ReturnType<typeof mountFeed>, index: number) =>
    wrapper.findAll('[data-test="memo-card"]')[index]

  /** Open a card's editor through the ⋯ menu (a popup teleported to body). */
  async function openEditorOn(wrapper: ReturnType<typeof mountFeed>, index: number) {
    await cardAt(wrapper, index).find('[data-test="memo-edit"]').trigger('click')
    await nextTick()
    const items = document.body.querySelectorAll<HTMLElement>('[data-test="memo-action-edit"]')
    items[items.length - 1].click()
    await nextTick()
  }

  const save = async (wrapper: ReturnType<typeof mountFeed>, index: number) => {
    await cardAt(wrapper, index).find('[data-test="edit-save"]').trigger('click')
    await flushPromises()
  }

  const inFlight = (wrapper: ReturnType<typeof mountFeed>, index: number) =>
    cardAt(wrapper, index).find('[data-test="edit-in-flight"]').exists()

  it('does not re-send an edit whose PATCH is still out when a SECOND memo is edited', async () => {
    useTimelineMock.mockReturnValue(
      feedResult([memoStub('a', 'memo A'), memoStub('b', 'memo B')]),
    )
    // Two PATCHes the test lands one at a time.
    const land: Record<string, () => void> = {}
    patch.mockImplementation(
      (_url: string, options: { params: { path: { uid: string } } }) =>
        new Promise((resolve) => {
          land[options.params.path.uid] = () => resolve({ error: undefined })
        }),
    )
    const wrapper = mountFeed()

    await openEditorOn(wrapper, 0)
    await save(wrapper, 0)
    expect(patch).toHaveBeenCalledTimes(1)

    await openEditorOn(wrapper, 1)
    await save(wrapper, 1)
    expect(patch).toHaveBeenCalledTimes(2) // A still out, B now out too

    // A's request has NOT landed — reopening its editor must not offer to send it again. (This is
    // where a single "which uid is the observer on" answer fails: starting B moved it off A.)
    await openEditorOn(wrapper, 0)
    expect(inFlight(wrapper, 0)).toBe(true)
    await save(wrapper, 0)
    expect(patch).toHaveBeenCalledTimes(2)
    // …and the refusal leaves the panel open, so the person sees why rather than losing the sheet
    expect(cardAt(wrapper, 0).find('[data-test="memo-editor"]').exists()).toBe(true)

    // A lands; B is still out. The panel A left open is editable again, B's card is not.
    land.a()
    await flushPromises()
    await nextTick()
    expect(inFlight(wrapper, 0)).toBe(false)
    await save(wrapper, 0)
    expect(patch).toHaveBeenCalledTimes(3)

    await openEditorOn(wrapper, 1)
    expect(inFlight(wrapper, 1)).toBe(true) // B's own PATCH is still on the wire
    await save(wrapper, 1)
    expect(patch).toHaveBeenCalledTimes(3) // …so B still refuses

    land.b()
    land.a()
    await flushPromises()
  })
})
