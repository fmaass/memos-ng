import type { QueryClient } from '@tanstack/vue-query'
import { useQueryClient } from '@tanstack/vue-query'
import { getCurrentInstance, onUnmounted } from 'vue'

import type { components } from '@/api/schema'
import { hasInFlight, nextAction, type TimelineData } from '@/lib/sseLifecycle'

export interface MediaEvent {
  uid: string
  status: components['schemas']['AttachmentStatus']
  progress_pct?: number
}

// Pure: return a new infinite-query data object with the event's attachment status applied.
// Exported so the cache transform is unit-tested without a live cache or EventSource.
export function applyMediaEvent<T extends TimelineData>(data: T, ev: MediaEvent): T {
  return {
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      items: page.items.map((memo) => ({
        ...memo,
        attachments: memo.attachments?.map((a) =>
          a.uid === ev.uid ? { ...a, status: ev.status } : a,
        ),
      })),
    })),
  }
}

interface MediaEventSource {
  onopen: ((event: Event) => void) | null
  onmessage: ((event: MessageEvent) => void) | null
  onerror: ((event: Event) => void) | null
  close: () => void
}

export type MediaEventSourceFactory = (url: string) => MediaEventSource

function anyMemoCacheInFlight(queryClient: QueryClient): boolean {
  return queryClient
    .getQueryCache()
    .findAll({ queryKey: ['memos'] })
    .some((query) => hasInFlight(query.state.data as TimelineData | undefined))
}

export function startMediaEvents(
  queryClient: QueryClient,
  createSource: MediaEventSourceFactory,
): () => void {
  let source: MediaEventSource | null = null
  let connected = false
  let everConnected = false
  let disposed = false

  const closeSource = () => {
    source?.close()
    source = null
    connected = false
  }

  const evaluate = () => {
    if (disposed) return
    const inFlight = anyMemoCacheInFlight(queryClient)
    const action = nextAction({ connected, inFlight, everConnected })
    if (action === 'close') {
      closeSource()
      return
    }
    if (action !== 'open') return

    source = createSource('/api/events')
    connected = true
    source.onopen = () => {
      if (everConnected) {
        void queryClient.invalidateQueries({ queryKey: ['memos'] })
      }
      everConnected = true
    }
    source.onmessage = (event: MessageEvent) => {
      let parsed: MediaEvent
      try {
        parsed = JSON.parse(event.data)
      } catch {
        return // ignore malformed events
      }
      // Patch EVERY memos feed cache (home, explore, search, archived) — they key off the filter
      // (['memos', {…}] / ['memos','timeline']), so a single exact key would miss all but home.
      queryClient.setQueriesData({ queryKey: ['memos'] }, (old: TimelineData | undefined) =>
        old?.pages ? applyMediaEvent(old, parsed) : old,
      )
    }
    source.onerror = () => {
      closeSource()
      evaluate()
    }
  }

  const unsubscribe = queryClient.getQueryCache().subscribe(evaluate)
  evaluate()

  return () => {
    disposed = true
    closeSource()
    unsubscribe()
  }
}

// Subscribe to the media SSE channel and patch the timeline cache as attachments advance
// (pending → processing → ready/failed) so cards update live without a reload.
export function useMediaEvents() {
  const queryClient = useQueryClient()
  if (typeof EventSource === 'undefined') return

  const stop = startMediaEvents(queryClient, (url) => new EventSource(url))

  if (getCurrentInstance()) onUnmounted(stop)
}
