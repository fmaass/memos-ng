import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { type LibraryFilter, useAttachmentLibrary } from '@/composables/useAttachmentLibrary'

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

function mountLibrary(initial: LibraryFilter = 'all') {
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
    expect(sent(0).searchParams.get('limit')).toBe('200')
    expect(lib.hasNextPage.value).toBe(true)

    await lib.fetchNextPage()
    await flushPromises()

    expect(transport).toHaveBeenCalledTimes(2)
    expect(sent(1).searchParams.get('cursor')).toBe('cur-1')
    expect(sent(1).searchParams.get('limit')).toBe('200')
    expect(lib.items.value.map((i) => i.uid)).toEqual(['a', 'b', 'c'])
    // No header on the last page: nothing further to ask for.
    expect(lib.hasNextPage.value).toBe(false)
  })

  it('keys each filter separately and sends it with every page', async () => {
    transport.mockResolvedValueOnce(ok(['a'])).mockResolvedValueOnce(ok(['v'], 'cur-v'))
    const { lib, filter } = mountLibrary()
    await flushPromises()
    expect(sent(0).searchParams.has('type')).toBe(false)

    filter.value = 'video'
    await flushPromises()

    expect(sent(1).searchParams.get('type')).toBe('video')
    expect(sent(1).searchParams.has('cursor')).toBe(false)
    expect(lib.items.value.map((i) => i.uid)).toEqual(['v'])
  })

  it("is reached by every invalidation of ['attachments'] (delete, print-master changes)", async () => {
    transport.mockResolvedValueOnce(ok(['a'])).mockResolvedValueOnce(ok(['b']))
    const { lib, queryClient } = mountLibrary('image')
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
