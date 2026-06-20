import { useQueryClient } from '@tanstack/vue-query'
import { getCurrentInstance, onUnmounted } from 'vue'

export interface MediaEvent {
  uid: string
  status: string
  progress_pct?: number
}

interface AttachmentLike {
  uid: string
  status: string
}
interface MemoLike {
  attachments?: AttachmentLike[]
}
interface TimelineData {
  pages: { items: MemoLike[] }[]
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

// Subscribe to the media SSE channel and patch the timeline cache as attachments advance
// (pending → processing → ready/failed) so cards update live without a reload.
export function useMediaEvents() {
  const queryClient = useQueryClient()
  if (typeof EventSource === 'undefined') return

  const source = new EventSource('/api/events')
  source.onmessage = (event: MessageEvent) => {
    let parsed: MediaEvent
    try {
      parsed = JSON.parse(event.data)
    } catch {
      return // ignore malformed events
    }
    queryClient.setQueryData(['memos', 'timeline'], (old: TimelineData | undefined) =>
      old ? applyMediaEvent(old, parsed) : old,
    )
  }

  if (getCurrentInstance()) onUnmounted(() => source.close())
}
