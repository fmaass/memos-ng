import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useAttachmentBuckets, useAttachmentLibrary, useAttachmentTags } from '@/composables/useAttachmentLibrary'
import type { BucketSize, LibraryFilter, LibraryKind } from '@/lib/libraryFilter'

// The real composable and the real generated client over a scripted transport: the assertions are
// on the requests the client actually sends, not on items a fake query hands back.
const { transport } = vi.hoisted(() => ({
  transport: vi.fn<(req: Request) => Promise<Response>>(),
}))
vi.mock('@/composables/useApi', async () => {
  const { default: createClient } = await import('openapi-fetch')
  const { authMiddleware, reachabilityMiddleware } = await import('@/api/client')
  const client = createClient({ baseUrl: '/', fetch: (req: Request) => transport(req) })
  client.use(authMiddleware)
  client.use(reachabilityMiddleware)
  return { useApi: () => client }
})

function ok(uids: string[], cursor?: string): Response {
  return new Response(JSON.stringify(uids.map((uid) => ({ uid }))), {
    status: 200,
    headers: { 'Content-Type': 'application/json', ...(cursor ? { 'X-Next-Cursor': cursor } : {}) },
  })
}
const sent = (n: number) => new URL(transport.mock.calls[n]![0].url)

function mountLibrary(initial: LibraryFilter = { order: 'newest' }) {
  const filter = ref<LibraryFilter>(initial)
  let lib!: ReturnType<typeof useAttachmentLibrary>
  const Host = defineComponent({
    setup() {
      lib = useAttachmentLibrary(filter)
      return () => h('div')
    },
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  mount(Host, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } })
  return { lib, filter, queryClient }
}

describe('useAttachmentLibrary', () => {
  beforeEach(() => transport.mockReset())

  it('sends the first page without a cursor and the second with the header it was given', async () => {
    transport.mockResolvedValueOnce(ok(['a', 'b'], 'cur-1')).mockResolvedValueOnce(ok(['c']))
    const { lib } = mountLibrary()
    await flushPromises()

    expect(sent(0).searchParams.has('cursor')).toBe(false)
    expect(sent(0).searchParams.get('limit')).toBe('500')
    expect(lib.hasNextPage.value).toBe(true)

    await lib.fetchNextPage()
    await flushPromises()

    expect(transport).toHaveBeenCalledTimes(2)
    expect(sent(1).searchParams.get('cursor')).toBe('cur-1')
    expect(sent(1).searchParams.get('limit')).toBe('500')
    expect(lib.items.value.map((i) => i.uid)).toEqual(['a', 'b', 'c'])
    // No header on the last page: nothing further to ask for.
    expect(lib.hasNextPage.value).toBe(false)
  })

  it('keys each filter separately and sends it with every page', async () => {
    transport.mockResolvedValueOnce(ok(['a'])).mockResolvedValueOnce(ok(['v'], 'cur-v'))
    const { lib, filter } = mountLibrary()
    await flushPromises()
    expect(sent(0).searchParams.has('type')).toBe(false)

    filter.value = { type: 'video', order: 'newest' }
    await flushPromises()

    expect(sent(1).searchParams.get('type')).toBe('video')
    expect(sent(1).searchParams.has('cursor')).toBe(false)
    expect(lib.items.value.map((i) => i.uid)).toEqual(['v'])
  })

  it('walks the library by takenAt and forwards the kind and the tag with every page', async () => {
    transport.mockResolvedValueOnce(ok(['a'], 'cur-1')).mockResolvedValueOnce(ok(['b']))
    const { lib } = mountLibrary({ type: 'video', order: 'oldest', tag: 'recap' })
    await flushPromises()
    await lib.fetchNextPage()
    await flushPromises()

    for (const n of [0, 1]) {
      expect(sent(n).searchParams.get('order')).toBe('taken_asc')
      expect(sent(n).searchParams.get('type')).toBe('video')
      expect(sent(n).searchParams.get('tag')).toBe('recap')
      expect(sent(n).searchParams.get('limit')).toBe('500')
    }
    expect(sent(1).searchParams.get('cursor')).toBe('cur-1')
  })

  it('asks for newest first by takenAt when nothing else is set', async () => {
    transport.mockResolvedValueOnce(ok(['a']))
    mountLibrary()
    await flushPromises()

    expect(sent(0).searchParams.get('order')).toBe('taken_desc')
    expect(sent(0).searchParams.has('type')).toBe(false)
    expect(sent(0).searchParams.has('tag')).toBe(false)
  })

  it("never serves one tag's pages under another: a tag switch is a fresh first page", async () => {
    transport.mockResolvedValueOnce(ok(['r1'], 'cur-r')).mockResolvedValueOnce(ok(['h1']))
    const { lib, filter } = mountLibrary({ order: 'newest', tag: 'recap' })
    await flushPromises()

    filter.value = { order: 'newest', tag: 'holiday' }
    await flushPromises()

    expect(sent(1).searchParams.get('tag')).toBe('holiday')
    expect(sent(1).searchParams.has('cursor')).toBe(false)
    expect(lib.items.value.map((i) => i.uid)).toEqual(['h1'])
  })

  it("is reached by every invalidation of ['attachments'] (delete, print-master changes)", async () => {
    transport.mockResolvedValueOnce(ok(['a'])).mockResolvedValueOnce(ok(['b']))
    const { lib, queryClient } = mountLibrary({ type: 'image', order: 'newest' })
    await flushPromises()

    await queryClient.invalidateQueries({ queryKey: ['attachments'] })
    await flushPromises()

    expect(transport).toHaveBeenCalledTimes(2)
    expect(sent(1).searchParams.get('type')).toBe('image')
    expect(lib.items.value.map((i) => i.uid)).toEqual(['b'])
  })

  it('surfaces a server error as an error, not as an empty library', async () => {
    transport.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'no' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 200 })))
    try {
      const { lib } = mountLibrary()
      await flushPromises()
      expect(lib.isError.value).toBe(true)
      expect(lib.items.value).toEqual([])
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

function tagCounts(rows: { name: string; count: number }[]): Response {
  return new Response(JSON.stringify(rows), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function mountTags(initialType: LibraryKind | undefined, initialEnabled: boolean) {
  const type = ref<LibraryKind | undefined>(initialType)
  const enabled = ref(initialEnabled)
  let tags!: ReturnType<typeof useAttachmentTags>
  const Host = defineComponent({
    setup() {
      tags = useAttachmentTags(type, enabled)
      return () => h('div')
    },
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  mount(Host, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } })
  return { tags, type, enabled, queryClient }
}

describe('useAttachmentTags', () => {
  beforeEach(() => transport.mockReset())

  it('asks for nothing until it is enabled (the menu is closed at rest)', async () => {
    transport.mockResolvedValueOnce(tagCounts([{ name: 'recap', count: 3 }]))
    const { tags, enabled } = mountTags(undefined, false)
    await flushPromises()
    expect(transport).not.toHaveBeenCalled()

    enabled.value = true
    await flushPromises()

    expect(sent(0).pathname).toBe('/api/attachments/tags')
    expect(sent(0).searchParams.has('type')).toBe(false)
    expect(tags.data.value).toEqual([{ name: 'recap', count: 3 }])
  })

  it('counts per kind: the kind is sent and keyed, so Videos never shows the photo counts', async () => {
    transport
      .mockResolvedValueOnce(tagCounts([{ name: 'recap', count: 9 }]))
      .mockResolvedValueOnce(tagCounts([{ name: 'recap', count: 2 }]))
    const { tags, type, queryClient } = mountTags(undefined, true)
    await flushPromises()

    type.value = 'video'
    await flushPromises()

    expect(sent(1).searchParams.get('type')).toBe('video')
    expect(tags.data.value).toEqual([{ name: 'recap', count: 2 }])
    expect(queryClient.getQueryData(['attachments', 'tags', 'video'])).toEqual([
      { name: 'recap', count: 2 },
    ])
  })

  it("is reached by every invalidation of ['attachments'] (a delete changes the counts)", async () => {
    transport
      .mockResolvedValueOnce(tagCounts([{ name: 'recap', count: 3 }]))
      .mockResolvedValueOnce(tagCounts([{ name: 'recap', count: 2 }]))
    const { tags, queryClient } = mountTags('image', true)
    await flushPromises()

    await queryClient.invalidateQueries({ queryKey: ['attachments'] })
    await flushPromises()

    expect(transport).toHaveBeenCalledTimes(2)
    expect(sent(1).searchParams.get('type')).toBe('image')
    expect(tags.data.value).toEqual([{ name: 'recap', count: 2 }])
  })
})

describe('useAttachmentBuckets', () => {
  beforeEach(() => transport.mockReset())

  const bucket = (key: string, count: number) => ({
    key,
    count,
    coverUid: `c-${key}`,
    coverThumbnailUrl: `/api/attachments/c-${key}/thumb`,
  })
  function buckets(body: unknown[]): Response {
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  function mountBuckets(size: BucketSize, initial: LibraryFilter, on = true) {
    const filter = ref<LibraryFilter>(initial)
    const enabled = ref(on)
    let result!: ReturnType<typeof useAttachmentBuckets>
    const Host = defineComponent({
      setup() {
        result = useAttachmentBuckets(size, filter, enabled)
        return () => h('div')
      },
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mount(Host, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } })
    return { result, filter, enabled, queryClient }
  }

  it('asks for the size in Zurich with the kind and the tag, and never an order', async () => {
    transport.mockResolvedValueOnce(buckets([bucket('2026-09-01', 3)]))
    const { result } = mountBuckets('day', { type: 'video', order: 'oldest', tag: 'recap' })
    await flushPromises()

    expect(transport).toHaveBeenCalledTimes(1)
    const url = sent(0)
    expect(url.pathname).toBe('/api/attachments/buckets')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      size: 'day',
      tz: 'Europe/Zurich',
      type: 'video',
      tag: 'recap',
    })
    expect(result.data.value).toEqual([bucket('2026-09-01', 3)])
  })

  it('asks for nothing while it is not enabled (the All view needs no buckets)', async () => {
    const { enabled } = mountBuckets('year', { order: 'newest' }, false)
    await flushPromises()
    expect(transport).not.toHaveBeenCalled()

    transport.mockResolvedValueOnce(buckets([bucket('2026', 9)]))
    enabled.value = true
    await flushPromises()
    expect(Object.fromEntries(sent(0).searchParams)).toEqual({ size: 'year', tz: 'Europe/Zurich' })
  })

  it("lives under ['attachments', 'buckets', size, filter] and is reached by every invalidation of ['attachments']", async () => {
    transport
      .mockResolvedValueOnce(buckets([bucket('2026-09', 3)]))
      .mockResolvedValueOnce(buckets([bucket('2026-09', 2)]))
    const { result, queryClient } = mountBuckets('month', { order: 'newest', tag: 'recap' })
    await flushPromises()
    expect(queryClient.getQueryData(['attachments', 'buckets', 'month', { order: 'newest', tag: 'recap' }])).toEqual([
      bucket('2026-09', 3),
    ])

    await queryClient.invalidateQueries({ queryKey: ['attachments'] })
    await flushPromises()

    expect(transport).toHaveBeenCalledTimes(2)
    expect(result.data.value).toEqual([bucket('2026-09', 2)])
  })

  it('surfaces a server error as an error, not as an empty set of buckets', async () => {
    transport.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'nope' }), { status: 422, headers: { 'Content-Type': 'application/json' } }),
    )
    const { result } = mountBuckets('day', { order: 'newest' })
    await flushPromises()
    expect(result.isError.value).toBe(true)
    expect(result.data.value).toBeUndefined()
  })
})
