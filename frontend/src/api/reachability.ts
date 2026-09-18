import { onlineManager } from '@tanstack/vue-query'

import { classifyStatus, PROBE_TIMEOUT_MS } from '@/lib/reachability'
import { createReachabilityMonitor, type ProbeResult } from '@/lib/reachabilityMonitor'

// The one anonymous route the app can ask "are you there?" without a session (app/main.py).
// `cache: 'no-store'` because a cached answer would be a lie about the present, and the service
// worker precaches only built assets, so nothing else can answer this from disk either.
// `credentials: 'omit'` keeps the probe from touching the rolling BFF session cookie: a probe
// every few seconds would otherwise keep a session alive forever on an idle tab.
export async function probeHealth(): Promise<ProbeResult> {
  try {
    const response = await fetch('/health', {
      cache: 'no-store',
      credentials: 'omit',
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    })
    // A proxy answering 502/503/504 means the app behind it is NOT there — including /health's
    // own 503, which it returns when the database is unreachable and the app cannot serve.
    return classifyStatus(response.status) === 'gateway-error' ? 'probe-failed' : 'probe-ok'
  } catch {
    return 'probe-failed'
  }
}

/** The app-wide reachability verdict. Inert until `installReachability()` wires it to the browser. */
export const reachability = createReachabilityMonitor({
  now: () => Date.now(),
  setTimer: (fn, delayMs) => setTimeout(fn, delayMs),
  clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  probe: probeHealth,
  onRecover: () => onlineManager.setOnline(true),
})

let installed = false

/**
 * Bind the verdict to the browser and to TanStack Query. Called once from main.ts — kept out of
 * module scope so importing the API client (which every test does) adds no listeners.
 */
export function installReachability(): void {
  if (installed || typeof window === 'undefined') return
  installed = true

  // Take TanStack Query's online state off `navigator.onLine` entirely: an empty setup replaces
  // its window online/offline listeners, so the only thing that can pause or resume queries is
  // this monitor's verdict, which is built from traffic that actually reached the server.
  onlineManager.setEventListener(() => undefined)
  onlineManager.setOnline(reachability.status() !== 'unreachable')
  reachability.subscribe((status) => onlineManager.setOnline(status !== 'unreachable'))

  // The browser regaining its own connection is not proof the server is back, but it is the best
  // moment to find out.
  window.addEventListener('online', () => reachability.report('browser-online'))

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') reachability.pause()
    else reachability.resume()
  })
}
