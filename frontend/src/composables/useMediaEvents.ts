import type { QueryClient } from '@tanstack/vue-query'
import { useQueryClient } from '@tanstack/vue-query'
import { getCurrentInstance, onUnmounted } from 'vue'

import type { components } from '@/api/schema'
import { hasInFlight, isTerminalStatus, nextAction, type TimelineData } from '@/lib/sseLifecycle'

type Attachment = components['schemas']['AttachmentRef']

// The cached shape of an attachment the SSE channel has touched. progress_pct is deliberately NOT
// part of AttachmentRef: the feed endpoints never serve it (computing every attachment's live
// transcode percentage on every timeline page would be a join in the hottest read path for a
// number that is stale a second later). It arrives only on the event stream, is carried on the
// cached object, and is read from there by the tile — see MediaTile's `progress_pct`.
export type CachedAttachment = Attachment & { progress_pct?: number }

export interface MediaEvent {
  uid: string
  status: components['schemas']['AttachmentStatus']
  progress_pct?: number
  // The terminal `ready` event carries the metadata the worker has just written.
  width?: number | null
  height?: number | null
  blurhash?: string | null
}

// What an event contributes to the cached attachment beyond its status. Dims are taken as a PAIR —
// one of them beside the other's stale value would describe an aspect ratio neither source has, and
// that ratio is exactly what the tile box is laid out from.
function metadataPatch(ev: MediaEvent): Partial<Attachment> {
  const patch: Partial<Attachment> = {}
  if (ev.width != null && ev.height != null) {
    patch.width = ev.width
    patch.height = ev.height
  }
  if (ev.blurhash != null) patch.blurhash = ev.blurhash
  return patch
}

// What one event makes of one attachment: its status, the metadata it carries, and — the reason
// the event has a percentage at all — how far the work has actually got. Dropping progress_pct
// here is why a transcoding video's tile sat at 0% for the whole transcode (MEMNG-36). Absent is
// distinct from 0: an event without a percentage leaves the last known one in place rather than
// resetting the tile to the start.
//
// An IN-FLIGHT event never applies to an attachment already cached as ready/failed. The stream is
// a poll, and a reconnect (or a slow client) can deliver a `processing` snapshot taken before the
// job finished AFTER the terminal event — applying it would put a finished photo back under a
// progress overlay that nothing will ever clear, since the job it describes is over and will
// report nothing further. Terminal events still apply to terminal state (a retried `failed`
// attachment may legitimately reach `ready`).
function patchAttachment(att: CachedAttachment, ev: MediaEvent): CachedAttachment {
  if (isTerminalStatus(att.status) && !isTerminalStatus(ev.status)) return att
  const next: CachedAttachment = { ...att, status: ev.status, ...metadataPatch(ev) }
  if (ev.progress_pct != null) next.progress_pct = ev.progress_pct
  return next
}

// Pure: return a new infinite-query data object with the event's attachment status AND the metadata
// it carries applied. Patching only the status left a just-finished upload at NULL dims in the
// cache, so its tile kept the fallback box and served no srcset until something refetched the feed.
// Exported so the cache transform is unit-tested without a live cache or EventSource.
export function applyMediaEvent<T extends TimelineData>(data: T, ev: MediaEvent): T {
  return {
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      items: page.items.map((memo) => ({
        ...memo,
        attachments: memo.attachments?.map((a) => (a.uid === ev.uid ? patchAttachment(a, ev) : a)),
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

// Delay before re-opening after a connection error — without it, a persistent failure
// while work is in-flight (server mid-restart, expired session) re-creates EventSources
// at network-RTT speed, a worse hammering than the native reconnect this module replaces.
export const RECONNECT_DELAY_MS = 3000

export function startMediaEvents(
  queryClient: QueryClient,
  createSource: MediaEventSourceFactory,
): () => void {
  let source: MediaEventSource | null = null
  let connected = false
  let everConnected = false
  let disposed = false
  let retryTimer: ReturnType<typeof setTimeout> | null = null

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
      if (retryTimer !== null) return
      retryTimer = setTimeout(() => {
        retryTimer = null
        evaluate()
      }, RECONNECT_DELAY_MS)
    }
  }

  const unsubscribe = queryClient.getQueryCache().subscribe(evaluate)
  evaluate()

  return () => {
    disposed = true
    closeSource()
    if (retryTimer !== null) clearTimeout(retryTimer)
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
