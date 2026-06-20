// Pure mapping from a push payload (sent by the worker's send_to_user) to the
// notification the service worker shows + where a click should land. Kept out of the
// service worker so it is unit-testable without a ServiceWorkerGlobalScope.
export interface PushPayload {
  uid?: string
  status?: 'ready' | 'failed' | string
  title?: string
}

export interface NotificationSpec {
  title: string
  body: string
  url: string
}

export function notificationFor(payload: PushPayload): NotificationSpec {
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
