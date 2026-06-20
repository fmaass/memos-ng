import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getMock = vi.fn()
const postMock = vi.fn()
vi.mock('@/composables/useApi', () => ({
  useApi: () => ({ GET: getMock, POST: postMock }),
}))

import { urlBase64ToUint8Array, usePushSubscription } from '@/composables/usePushSubscription'

function grantBrowserPush(permission: 'granted' | 'denied', subscribe = vi.fn()) {
  Object.defineProperty(navigator, 'serviceWorker', {
    value: { ready: Promise.resolve({ pushManager: { subscribe } }) },
    configurable: true,
  })
  ;(window as unknown as { PushManager: unknown }).PushManager = function () {}
  vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue(permission) })
}

describe('urlBase64ToUint8Array', () => {
  it('decodes base64 to the right bytes', () => {
    expect(Array.from(urlBase64ToUint8Array('AQID'))).toEqual([1, 2, 3])
  })
  it('tolerates base64url chars and missing padding', () => {
    expect(urlBase64ToUint8Array('AQIDBA').length).toBeGreaterThan(0)
  })
})

describe('usePushSubscription', () => {
  beforeEach(() => {
    getMock.mockReset()
    postMock.mockReset()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    delete (window as unknown as { PushManager?: unknown }).PushManager
    delete (navigator as unknown as { serviceWorker?: unknown }).serviceWorker
  })

  it('enable() grants permission, fetches the VAPID key, subscribes and registers it', async () => {
    const subscribe = vi.fn().mockResolvedValue({
      toJSON: () => ({ endpoint: 'https://push/abc', keys: { p256dh: 'P', auth: 'A' } }),
    })
    grantBrowserPush('granted', subscribe)
    getMock.mockResolvedValue({ data: { key: 'AQID' }, error: undefined })
    postMock.mockResolvedValue({ data: {}, error: undefined })

    const { enable, enabled, supported } = usePushSubscription()
    expect(supported).toBe(true)

    expect(await enable()).toBe(true)
    expect(enabled.value).toBe(true)
    expect(getMock).toHaveBeenCalledWith('/api/push/vapid-public-key')
    expect(subscribe).toHaveBeenCalledWith(expect.objectContaining({ userVisibleOnly: true }))
    expect(postMock).toHaveBeenCalledWith('/api/push/subscribe', {
      body: { endpoint: 'https://push/abc', keys: { p256dh: 'P', auth: 'A' } },
    })
  })

  it('enable() no-ops when permission is denied', async () => {
    grantBrowserPush('denied')
    const { enable, enabled } = usePushSubscription()
    expect(await enable()).toBe(false)
    expect(enabled.value).toBe(false)
    expect(getMock).not.toHaveBeenCalled()
    expect(postMock).not.toHaveBeenCalled()
  })

  it('reports unsupported when the browser lacks PushManager', () => {
    // no serviceWorker/PushManager defined → unsupported
    const { supported } = usePushSubscription()
    expect(supported).toBe(false)
  })
})
