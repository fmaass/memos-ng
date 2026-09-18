import {
  initialReachabilityState,
  probeDelayMs,
  provesReachable,
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
  // Bumped on EVERY piece of evidence that the server answered, not only on the transition back
  // to `reachable`. A probe carries the generation it was started in, so one still in flight when
  // something newer answered is discarded rather than being allowed to fail the server on a
  // verdict about a moment that has passed. Bumping only on the transition left the probes that
  // start without one — a stream error asking its question while already reachable — unprotected.
  let generation = 0
  const listeners = new Set<(status: ReachabilityStatus) => void>()

  const cancelTimer = () => {
    if (timer === null) return
    deps.clearTimer(timer)
    timer = null
  }

  const schedule = (delayMs: number) => {
    // A pending probe keeps its deadline. Rescheduling on every new failure let a stream of
    // failures arriving faster than the backoff push the probe out for ever, so the server was
    // never asked again and the banner stayed up after it came back.
    if (paused || disposed || probing || timer !== null) return
    timer = deps.setTimer(() => {
      timer = null
      void runProbe()
    }, delayMs)
  }

  const runProbe = async () => {
    // One probe in flight at a time: several failing queries land together, and each of them
    // asking for its own probe would multiply exactly the traffic that is already failing.
    if (probing || paused || disposed) return
    probing = true
    const startedIn = generation
    let result: ReachabilitySignal
    try {
      result = await deps.probe()
    } catch {
      result = 'probe-failed'
    }
    probing = false
    if (disposed) return
    if (startedIn !== generation) {
      // The answer is about a moment that has passed, so it may not be reported. But if the
      // server has stopped answering AGAIN since this probe left, dropping it silently would
      // leave nothing pending at all: every `probe-now` and `probe-in` the new outage emitted
      // was skipped precisely BECAUSE this probe was in flight. Once the state reaches
      // `unreachable` the query layer is paused, so no other traffic would ever wake it and the
      // banner would stay up until someone pressed Retry. A timer rather than an immediate
      // probe: the new outage's own failures have already earned their backoff, and jumping it
      // would hammer a server that has just shown it can answer.
      if (state.status !== 'reachable') schedule(probeDelayMs(state.failures - 1))
      return
    }
    report(result)
  }

  const apply = (effects: ReachabilityEffect[]) => {
    for (const effect of effects) {
      if (effect.type === 'stop-probing') {
        cancelTimer()
      } else if (effect.type === 'probe-now') {
        // A probe already on its way IS the immediate answer; cancelling the schedule for one
        // that will not start would leave nothing pending at all.
        if (probing) continue
        cancelTimer()
        void runProbe()
      } else if (effect.type === 'probe-in') {
        schedule(effect.delayMs)
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
    // After the staleness check in runProbe, never before it: a probe's own `probe-ok` must not
    // be discarded by the very bump it causes.
    if (provesReachable(signal)) generation++
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
