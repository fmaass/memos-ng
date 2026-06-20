/// <reference lib="webworker" />
import { clientsClaim } from 'workbox-core'
import { precacheAndRoute } from 'workbox-precaching'

import { notificationFor, type PushPayload } from '@/lib/pushNotification'

declare const self: ServiceWorkerGlobalScope

// App-shell precache (injected by vite-plugin-pwa at build) + take control immediately
// so a new version serves on next load. /api/* and /file/* are never precached (the
// glob only matches built assets), so API calls always hit the network.
self.skipWaiting()
clientsClaim()
precacheAndRoute(self.__WB_MANIFEST)

// Web Push receiver — the worker's send_to_user posts {uid,status,title}.
self.addEventListener('push', (event: PushEvent) => {
  let payload: PushPayload = {}
  try {
    payload = event.data?.json() ?? {}
  } catch {
    payload = { title: event.data?.text() }
  }
  const { title, body, url } = notificationFor(payload)
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: '/icons/icon-192.png',
      badge: '/icons/icon-192.png',
      data: { url },
    }),
  )
})

// Clicking a notification focuses an open tab or deep-links into the app.
self.addEventListener('notificationclick', (event: NotificationEvent) => {
  event.notification.close()
  const url = (event.notification.data?.url as string) || '/'
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ('focus' in client) return client.focus()
      }
      return self.clients.openWindow(url)
    }),
  )
})
