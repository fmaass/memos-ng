import {
  initialReachabilityState,
  reduceReachability,
  type ReachabilityEffect,
  type ReachabilitySignal,
  type ReachabilityState,
  type ReachabilityStatus,
} from './reachability'

export type ProbeResult = 'probe-ok' | 'probe-failed'

export interface ReachabilityMonitorDeps {
  /** Injected so the debounce is tested without sleeping. */
  now: () => number
  setTimer: (fn: () => void, delayMs: number) => unknown
  clearTimer: (handle: unknown) => void
  /** One /health request. Rejecting counts as a failed probe. */
  probe: () => Promise<ProbeResult>
  /** Called when the server answers again after it had stopped answering. */
  onRecover?: () => void
}

export interface ReachabilityMonitor {
  report: (signal: ReachabilitySignal) => void
  retryNow: () => void
  status: () => ReachabilityStatus
  snapshot: () => ReachabilityState
  subscribe: (listener: (status: ReachabilityStatus) => void) => () => void
  /** Stop probing while nobody is looking (hidden tab); resume picks the probe straight back up. */
  pause: () => void
  resume: () => void
  dispose: () => void
}

/**
 * Drives the pure state machine: turns its effects into probes, timers and notifications.
 * Every side-effecting dependency is injected, so this is unit-tested with no browser and no
 * real clock.
 */
export function createReachabilityMonitor(deps: ReachabilityMonitorDeps): ReachabilityMonitor {
  let state = initialReachabilityState()
  let timer: unknown = null
  let probing = false
  let paused = false
  let disposed = false
  const listeners = new Set<(status: ReachabilityStatus) => void>()

  const cancelTimer = () => {
    if (timer === null) return
    deps.clearTimer(timer)
    timer = null
  }

  const runProbe = async () => {
    // One probe in flight at a time: several failing queries land together, and each of them
    // asking for its own probe would multiply exactly the traffic that is already failing.
    if (probing || paused || disposed) return
    probing = true
    let result: ReachabilitySignal
    try {
      result = await deps.probe()
    } catch {
      result = 'probe-failed'
    }
    probing = false
    if (disposed) return
    report(result)
  }

  const apply = (effects: ReachabilityEffect[]) => {
    for (const effect of effects) {
      if (effect.type === 'stop-probing') {
        cancelTimer()
      } else if (effect.type === 'probe-now') {
        cancelTimer()
        void runProbe()
      } else if (effect.type === 'probe-in') {
        cancelTimer()
        if (paused || disposed) continue
        timer = deps.setTimer(() => {
          timer = null
          void runProbe()
        }, effect.delayMs)
      } else if (effect.type === 'refetch') {
        deps.onRecover?.()
      }
    }
  }

  function report(signal: ReachabilitySignal): void {
    if (disposed) return
    const previous = state.status
    const decision = reduceReachability(state, signal, deps.now())
    state = decision.state
    apply(decision.effects)
    if (state.status !== previous) {
      for (const listener of listeners) listener(state.status)
    }
  }

  return {
    report,
    retryNow: () => report('retry-requested'),
    status: () => state.status,
    snapshot: () => state,
    subscribe: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    pause: () => {
      paused = true
      cancelTimer()
    },
    resume: () => {
      if (!paused) return
      paused = false
      if (state.status !== 'reachable') void runProbe()
    },
    dispose: () => {
      disposed = true
      cancelTimer()
      listeners.clear()
    },
  }
}
