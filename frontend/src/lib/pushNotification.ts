// Pure mapping from a push payload (sent through the server's notify seam) to the
// notification the service worker shows + where a click should land. Kept out of the
// service worker so it is unit-testable without a ServiceWorkerGlobalScope.
//
// Two payloads arrive: the per-file one ({uid,status,title}, no `kind`) and, since
// MEMNG-967, the memo-live one ({kind:'memo_live',uid,owner,title,attachments}).
export interface PushPayload {
  kind?: 'memo_live' | string
  uid?: string
  status?: 'ready' | 'failed' | string
  title?: string
  owner?: string
  attachments?: number
}

export interface NotificationSpec {
  title: string
  body: string
  url: string
}

function memoLive(payload: PushPayload): NotificationSpec {
  const n = payload.attachments ?? 0
  const files = n === 0 ? '' : n === 1 ? '1 file' : `${n} files`
  const body = [payload.title, files].filter(Boolean).join(' · ')
  return {
    title: `${payload.owner || 'memos-ng'} posted`,
    body,
    url: payload.uid ? `/m/${encodeURIComponent(payload.uid)}` : '/',
  }
}

export function notificationFor(payload: PushPayload): NotificationSpec {
  if (payload.kind === 'memo_live') return memoLive(payload)
  const name = payload.title || 'memos-ng'
  const body =
    payload.status === 'ready'
      ? 'Your media is ready'
      : payload.status === 'failed'
        ? 'Media processing failed'
        : 'Update available'
  // Deep-link to the originating memo/attachment when known, else home.
  const url = payload.uid ? `/?attachment=${encodeURIComponent(payload.uid)}` : '/'
  return { title: name, body, url }
}
