import { ref } from 'vue'

import { useApi } from '@/composables/useApi'

// VAPID keys are base64url; PushManager.subscribe wants the raw bytes. Allocated over an
// explicit ArrayBuffer so the type satisfies BufferSource (not SharedArrayBuffer). For tests.
export function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const normalized = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(normalized)
  const bytes = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
  return bytes
}

function isSupported(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    'serviceWorker' in navigator &&
    typeof window !== 'undefined' &&
    'PushManager' in window &&
    typeof Notification !== 'undefined'
  )
}

// Opt-in Web Push: ask permission, fetch the server's VAPID key, subscribe via the SW's
// PushManager, and register the subscription with the backend. No-ops (returns false)
// where push is unsupported or permission is denied — never throws into the caller.
export function usePushSubscription() {
  const api = useApi()
  const supported = isSupported()
  const enabled = ref(false)
  const busy = ref(false)

  async function enable(): Promise<boolean> {
    if (!supported) return false
    busy.value = true
    try {
      if ((await Notification.requestPermission()) !== 'granted') return false
      const { data, error } = await api.GET('/api/push/vapid-public-key')
      if (error || !data?.key) return false
      const registration = await navigator.serviceWorker.ready
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(data.key),
      })
      const json = subscription.toJSON()
      await api.POST('/api/push/subscribe', {
        body: {
          endpoint: json.endpoint!,
          keys: { p256dh: json.keys!.p256dh!, auth: json.keys!.auth! },
        },
      })
      enabled.value = true
      return true
    } finally {
      busy.value = false
    }
  }

  return { supported, enabled, busy, enable }
}
