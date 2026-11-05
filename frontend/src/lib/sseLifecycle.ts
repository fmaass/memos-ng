import type { InfiniteData } from '@tanstack/vue-query'

import type { components } from '@/api/schema'

type AttachmentStatus = components['schemas']['AttachmentStatus']
type MemoList = components['schemas']['MemoList']
export type TimelineData = InfiniteData<MemoList>

const IN_FLIGHT_STATUSES = new Set<AttachmentStatus>(['uploading', 'pending', 'processing'])

// The one place a status is classified as finished. `ready` and `failed` are the states the
// backend never moves an attachment out of on its own, so a client that has seen one must not be
// talked back out of it by a late in-flight event (see applyMediaEvent).
export function isTerminalStatus(status: AttachmentStatus): boolean {
  return !IN_FLIGHT_STATUSES.has(status)
}

export function hasInFlight(data: TimelineData | undefined): boolean {
  return (
    data?.pages.some((page) =>
      page.items.some((memo) =>
        memo.attachments.some((attachment) => IN_FLIGHT_STATUSES.has(attachment.status)),
      ),
    ) ?? false
  )
}

// An upload-queue item whose bytes are still to land. Its `upload.progress` frames (MEMNG-965) come
// only over the event stream, and a file picked from an idle timeline is in no memo cache yet — so
// the queue has to ask for the stream itself (MEMNG-1181). `done` is not active: its bytes have
// landed, and once the memo posts the cache's own in-flight attachment holds the stream.
export function hasActiveUpload(statuses: Iterable<string>): boolean {
  for (const status of statuses) {
    if (status === 'queued' || status === 'uploading') return true
  }
  return false
}

export function nextAction(state: {
  connected: boolean
  inFlight: boolean
  uploading: boolean
  everConnected: boolean
}): 'open' | 'close' | 'none' {
  void state.everConnected
  const wanted = state.inFlight || state.uploading
  if (wanted && !state.connected) return 'open'
  if (!wanted && state.connected) return 'close'
  return 'none'
}
