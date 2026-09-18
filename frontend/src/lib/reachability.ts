// Whether the server can be reached, decided from real traffic only.
//
// `navigator.onLine` is deliberately NOT an input: the failure this exists for is a dropped
// tunnel between this device and the server, during which the device's own network is perfectly
// up and `navigator.onLine` stays true. The only honest evidence is a request that did or did not
// get an answer, so every verdict here comes from a request outcome or from the /health probe.
//
// Pure and out of any component on purpose (same reason as sseLifecycle.ts): happy-dom has no
// fetch timing, no EventSource and no timers worth trusting, so the decision is unit-tested
// directly and the component only renders the answer.

export type ReachabilityStatus = 'reachable' | 'suspect' | 'unreachable'

/** One classified outcome. Every category the app can observe has its own name here. */
export type ReachabilitySignal =
  // The fetch never got an answer.
  | 'network-error'
  | 'timeout'
  // A proxy answered but the app behind it did not (502/503/504).
  | 'gateway-error'
  // The server answered 401 — an auth outcome, not a connectivity one.
  | 'unauthorized'
  // The server answered something else (any 2xx/3xx/4xx/5xx): it is reachable.
  | 'server-answered'
  | 'success'
  | 'probe-ok'
  | 'probe-failed'
  | 'stream-open'
  | 'stream-error'
  // A rejection that is neither a network failure nor a timeout (a bug in our own code, a
  // serializer throwing). It says nothing about the network, so it changes nothing.
  | 'other-error'
  // The browser's `online` event, and the banner's "Retry now": neither is a verdict, both are
  // a reason to probe straight away instead of waiting out the backoff.
  | 'browser-online'
  | 'retry-requested'

export type ReachabilityEffect =
  | { type: 'probe-now' }
  | { type: 'probe-in'; delayMs: number }
  | { type: 'stop-probing' }
  | { type: 'refetch' }

export interface ReachabilityState {
  status: ReachabilityStatus
  /** Failure signals in the current streak; reset the moment anything answers. */
  failures: number
  /** Clock reading of the first failure of the streak, or null while reachable. */
  streakStartedAt: number | null
}

// A single failed request is the normal cost of doing business — one dropped packet, a cancelled
// navigation, the app container restarting. The tunnel drop this banner exists for keeps failing,
// so the banner waits for a SECOND independent failure AND for real time to pass. Both conditions,
// not either: two failures inside a few milliseconds are usually one incident seen twice (a page
// firing several queries at once), while three seconds of nothing answering is not.
export const UNREACHABLE_AFTER_FAILURES = 2
export const UNREACHABLE_AFTER_MS = 3000

// The first probe goes out immediately on the first failure; from there each further failure
// doubles the wait, capped so a long outage settles into one cheap request every 30s rather than
// hammering a server that may be struggling.
export const PROBE_BASE_DELAY_MS = 1000
export const PROBE_MAX_DELAY_MS = 30000
/** A probe that has not answered in this long is itself evidence; keep it well under the cap. */
export const PROBE_TIMEOUT_MS = 4000

const FAILURE_SIGNALS = new Set<ReachabilitySignal>([
  'network-error',
  'timeout',
  'gateway-error',
  'probe-failed',
  'stream-error',
])

// Anything that proves the server answered. `unauthorized` belongs here: a 401 is a reply, and a
// reply is proof of reachability — it must never raise the banner, and it may clear it.
const ANSWERED_SIGNALS = new Set<ReachabilitySignal>([
  'success',
  'server-answered',
  'unauthorized',
  'probe-ok',
  'stream-open',
])

export function initialReachabilityState(): ReachabilityState {
  return { status: 'reachable', failures: 0, streakStartedAt: null }
}

/** The nth wait between probes: exponential, capped. `probeDelayMs(1)` is the first wait. */
export function probeDelayMs(step: number): number {
  const steps = Math.max(0, step - 1)
  if (steps > 20) return PROBE_MAX_DELAY_MS
  return Math.min(PROBE_BASE_DELAY_MS * 2 ** steps, PROBE_MAX_DELAY_MS)
}

/** What an HTTP status says about reachability. */
export function classifyStatus(status: number): ReachabilitySignal {
  if (status === 401) return 'unauthorized'
  if (status === 502 || status === 503 || status === 504) return 'gateway-error'
  return 'server-answered'
}

/** What a rejected fetch says about reachability. */
export function classifyFailure(error: unknown): ReachabilitySignal {
  if (error instanceof Error && (error.name === 'AbortError' || error.name === 'TimeoutError')) {
    return 'timeout'
  }
  if (error instanceof TypeError) return 'network-error'
  return 'other-error'
}

export interface ReachabilityDecision {
  state: ReachabilityState
  effects: ReachabilityEffect[]
}

/**
 * The whole decision, as a pure function of the previous state, one signal and the clock.
 * The clock is a parameter so the debounce is tested without sleeping.
 */
export function reduceReachability(
  state: ReachabilityState,
  signal: ReachabilitySignal,
  now: number,
): ReachabilityDecision {
  if (signal === 'other-error') return { state, effects: [] }

  if (signal === 'browser-online' || signal === 'retry-requested') {
    // Never a verdict of its own — the device being "online" says nothing about the tunnel.
    if (state.status === 'reachable') return { state, effects: [] }
    return { state, effects: [{ type: 'probe-now' }] }
  }

  if (ANSWERED_SIGNALS.has(signal)) {
    if (state.status === 'reachable') return { state, effects: [] }
    return {
      state: initialReachabilityState(),
      effects: [{ type: 'stop-probing' }, { type: 'refetch' }],
    }
  }

  if (!FAILURE_SIGNALS.has(signal)) return { state, effects: [] }

  const failures = state.failures + 1
  const streakStartedAt = state.streakStartedAt ?? now
  const debounced =
    failures >= UNREACHABLE_AFTER_FAILURES && now - streakStartedAt >= UNREACHABLE_AFTER_MS
  const next: ReachabilityState = {
    status: debounced ? 'unreachable' : 'suspect',
    failures,
    streakStartedAt,
  }
  // The first failure gets an immediate probe: that is what turns one ambiguous failure into the
  // second data point the debounce needs, without waiting for the user to touch anything. Every
  // failure after it waits — `failures - 1` because the first one did not wait at all, so the
  // first real wait is one second and the banner lands right at the end of the debounce window.
  const effects: ReachabilityEffect[] =
    state.status === 'reachable'
      ? [{ type: 'probe-now' }]
      : [{ type: 'probe-in', delayMs: probeDelayMs(failures - 1) }]
  return { state: next, effects }
}
