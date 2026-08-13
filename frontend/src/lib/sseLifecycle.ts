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

export function nextAction(state: {
  connected: boolean
  inFlight: boolean
  everConnected: boolean
}): 'open' | 'close' | 'none' {
  void state.everConnected
  if (state.inFlight && !state.connected) return 'open'
  if (!state.inFlight && state.connected) return 'close'
  return 'none'
}
