import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { components } from '@/api/schema'
import {
  applyMediaEvent,
  RECONNECT_DELAY_MS,
  startMediaEvents,
  useMediaEvents,
} from '@/composables/useMediaEvents'
import type { TimelineData } from '@/lib/sseLifecycle'

type AttachmentStatus = components['schemas']['AttachmentStatus']
type MemoOut = components['schemas']['MemoOut']

class FakeSource {
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  close = vi.fn()

  constructor(public url: string) {}
}

function timeline(status: AttachmentStatus, uid = 'a1'): TimelineData {
  return {
    pages: [
      {
        items: [
          {
            uid: 'm1',
            attachments: [{ uid, status }],
          } as MemoOut,
        ],
        nextCursor: null,
        totalCount: 1,
      },
    ],
    pageParams: [undefined],
  }
}

function client(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
}

describe('applyMediaEvent', () => {
  it('updates only the matching attachment status across pages', () => {
    const data = {
      pages: [
        { items: [{ attachments: [{ uid: 'x', status: 'processing' }, { uid: 'y', status: 'ready' }] }] },
      ],
      pageParams: [undefined],
    } as TimelineData
    const next = applyMediaEvent(data, { uid: 'x', status: 'ready' })
    const atts = next.pages[0].items[0].attachments
    expect(atts.find((a) => a.uid === 'x')!.status).toBe('ready')
    expect(atts.find((a) => a.uid === 'y')!.status).toBe('ready') // unchanged
    expect(data.pages[0].items[0].attachments[0].status).toBe('processing') // original untouched
  })
})

describe('startMediaEvents', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('opens exactly one connection when one of multiple memo caches becomes in-flight', () => {
    const queryClient = client()
    const sources: FakeSource[] = []
    const factory = vi.fn((url: string) => {
      const source = new FakeSource(url)
      sources.push(source)
      return source
    })

    startMediaEvents(queryClient, factory)
    queryClient.setQueryData(['memos', 'timeline'], timeline('ready', 'a1'))
    queryClient.setQueryData(['memos', { visibility: 'PUBLIC' }], timeline('processing', 'a2'))

    expect(factory).toHaveBeenCalledTimes(1)
    expect(sources[0].url).toBe('/api/events')
  })

  it('closes the connection when all memo caches become ready', () => {
    const queryClient = client()
    const sources: FakeSource[] = []
    startMediaEvents(queryClient, (url) => {
      const source = new FakeSource(url)
      sources.push(source)
      return source
    })

    queryClient.setQueryData(['memos', 'timeline'], timeline('pending', 'a1'))
    queryClient.setQueryData(['memos', { visibility: 'PUBLIC' }], timeline('processing', 'a2'))
    expect(sources).toHaveLength(1)

    queryClient.setQueryData(['memos', 'timeline'], timeline('ready', 'a1'))
    expect(sources[0].close).not.toHaveBeenCalled()
    queryClient.setQueryData(['memos', { visibility: 'PUBLIC' }], timeline('ready', 'a2'))
    expect(sources[0].close).toHaveBeenCalledTimes(1)
  })

  it('fires one catch-up invalidation per reconnect, never per message', () => {
    vi.useFakeTimers()
    const queryClient = client()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue()
    const sources: FakeSource[] = []
    startMediaEvents(queryClient, (url) => {
      const source = new FakeSource(url)
      sources.push(source)
      return source
    })

    queryClient.setQueryData(['memos', 'timeline'], timeline('processing'))
    sources[0].onopen?.(new Event('open'))
    expect(invalidate).not.toHaveBeenCalled()

    sources[0].onmessage?.(new MessageEvent('message', { data: '{"uid":"a1","status":"processing"}' }))
    expect(invalidate).not.toHaveBeenCalled()

    sources[0].onerror?.(new Event('error'))
    // Reopening is delayed — a persistent failure must not hammer the server.
    vi.advanceTimersByTime(RECONNECT_DELAY_MS - 1)
    expect(sources).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(sources).toHaveLength(2)
    sources[1].onopen?.(new Event('open'))
    expect(invalidate).toHaveBeenCalledTimes(1)
    expect(invalidate).toHaveBeenLastCalledWith({ queryKey: ['memos'] })

    sources[1].onmessage?.(
      new MessageEvent('message', { data: '{"uid":"a1","status":"processing"}' }),
    )
    expect(invalidate).toHaveBeenCalledTimes(1)

    sources[1].onerror?.(new Event('error'))
    vi.advanceTimersByTime(RECONNECT_DELAY_MS)
    sources[2].onopen?.(new Event('open'))
    expect(invalidate).toHaveBeenCalledTimes(2)
    vi.useRealTimers()
  })

  it('cancels a pending error-retry on teardown', () => {
    vi.useFakeTimers()
    const queryClient = client()
    queryClient.setQueryData(['memos', 'timeline'], timeline('processing'))
    const sources: FakeSource[] = []
    const stop = startMediaEvents(queryClient, (url) => {
      const source = new FakeSource(url)
      sources.push(source)
      return source
    })

    sources[0].onerror?.(new Event('error'))
    stop()
    vi.advanceTimersByTime(RECONNECT_DELAY_MS * 2)
    expect(sources).toHaveLength(1)
    vi.useRealTimers()
  })

  it('closes without reopening when an error arrives and nothing is in-flight', () => {
    const queryClient = client()
    const data = timeline('processing')
    queryClient.setQueryData(['memos', 'timeline'], data)
    const sources: FakeSource[] = []
    startMediaEvents(queryClient, (url) => {
      const source = new FakeSource(url)
      sources.push(source)
      return source
    })

    data.pages[0].items[0].attachments[0].status = 'ready'
    sources[0].onerror?.(new Event('error'))

    expect(sources[0].close).toHaveBeenCalledTimes(1)
    expect(sources).toHaveLength(1)
  })

  it('teardown closes the source and unsubscribes from cache changes', () => {
    const queryClient = client()
    const queryCache = queryClient.getQueryCache()
    const originalSubscribe = queryCache.subscribe.bind(queryCache)
    const unsubscribe = vi.fn()
    vi.spyOn(queryCache, 'subscribe').mockImplementation((listener) => {
      const stop = originalSubscribe(listener)
      return () => {
        stop()
        unsubscribe()
      }
    })
    const sources: FakeSource[] = []
    const stop = startMediaEvents(queryClient, (url) => {
      const source = new FakeSource(url)
      sources.push(source)
      return source
    })

    queryClient.setQueryData(['memos', 'timeline'], timeline('uploading'))
    expect(sources).toHaveLength(1)

    stop()
    queryClient.setQueryData(['memos', 'timeline'], timeline('processing'))

    expect(sources[0].close).toHaveBeenCalledTimes(1)
    expect(unsubscribe).toHaveBeenCalledTimes(1)
    expect(sources).toHaveLength(1)
  })
})

describe('useMediaEvents', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('does not subscribe when EventSource is unavailable', () => {
    vi.stubGlobal('EventSource', undefined)
    const queryClient = client()
    const subscribe = vi.spyOn(queryClient.getQueryCache(), 'subscribe')
    const Comp = defineComponent({
      setup() {
        useMediaEvents()
        return () => h('div')
      },
    })

    mount(Comp, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } })

    expect(subscribe).not.toHaveBeenCalled()
  })
})
