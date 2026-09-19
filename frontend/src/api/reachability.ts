import { onlineManager, type QueryClient } from '@tanstack/vue-query'

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

/** The app's query client, from `installReachability`. Null until the app has installed. */
let appQueryClient: QueryClient | null = null

/**
 * Everything one recovery does. Exported so the wiring is unit-tested against a real
 * `QueryClient` without mounting anything.
 *
 * `setOnline(true)` alone only resumes queries that were PAUSED, which needs the verdict to have
 * reached `unreachable`. A short blip that never got past `suspect` never changed the online
 * state, so that call is a no-op and a query with `retry: false` that failed during the blip is
 * left sitting in its error state for ever (the session query is the live example). Invalidating
 * the queries that are actually in error is what gets them asked again.
 */
export function recoverQueries(client: QueryClient | null): void {
  onlineManager.setOnline(true)
  if (client === null) return
  // Exactly one invalidation per recovery, and never a healthy query: the predicate picks only
  // the queries that are actually in error, and TanStack refetches the ACTIVE ones among them.
  // Deliberately NOT guarded against a previous invalidation still running — a recovery that
  // arrived while the last refetch was in flight is a new proof about a new moment, and skipping
  // it would put the app right back in the state this fixes. Rejections are swallowed: a refetch
  // that fails again is the query's own error to report, and it will feed the monitor a fresh
  // failure signal through the API client anyway.
  void client
    .invalidateQueries({ predicate: (query) => query.state.status === 'error' })
    .catch(() => undefined)
}

/** The app-wide reachability verdict. Inert until `installReachability()` wires it to the browser. */
export const reachability = createReachabilityMonitor({
  now: () => Date.now(),
  setTimer: (fn, delayMs) => setTimeout(fn, delayMs),
  clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  probe: probeHealth,
  onRecover: () => recoverQueries(appQueryClient),
})

let installed = false

/**
 * Bind the verdict to the browser and to TanStack Query. Called once from main.ts — kept out of
 * module scope so importing the API client (which every test does) adds no listeners.
 */
export function installReachability(queryClient: QueryClient): void {
  if (installed || typeof window === 'undefined') return
  installed = true
  appQueryClient = queryClient

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
