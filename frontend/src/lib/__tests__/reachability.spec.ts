import { describe, expect, it } from 'vitest'

import {
  classifyFailure,
  classifyStatus,
  initialReachabilityState,
  probeDelayMs,
  PROBE_MAX_DELAY_MS,
  reduceReachability,
  UNREACHABLE_AFTER_MS,
  type ReachabilitySignal,
  type ReachabilityState,
} from '@/lib/reachability'

function suspectAfter(signal: ReachabilitySignal, at = 0): ReachabilityState {
  return reduceReachability(initialReachabilityState(), signal, at).state
}

describe('classifyStatus', () => {
  it.each([502, 503, 504])('reads %d as the proxy answering for an app that did not', (status) => {
    expect(classifyStatus(status)).toBe('gateway-error')
  })

  it('reads 401 as an auth outcome, never a connectivity one', () => {
    expect(classifyStatus(401)).toBe('unauthorized')
  })

  it.each([200, 204, 304, 400, 404, 409, 422, 500])(
    'reads %d as the server having answered',
    (status) => {
      expect(classifyStatus(status)).toBe('server-answered')
    },
  )
})

describe('classifyFailure', () => {
  it('reads a TypeError (the shape a failed fetch rejects with) as a network error', () => {
    expect(classifyFailure(new TypeError('Failed to fetch'))).toBe('network-error')
  })

  it('reads a TimeoutError (what AbortSignal.timeout rejects with) as a timeout', () => {
    const error = new Error('timed out')
    error.name = 'TimeoutError'
    expect(classifyFailure(error)).toBe('timeout')
  })

  it('reads an AbortError as the app cancelling itself, which is no evidence at all', () => {
    // Query cancellation, a navigation, a component unmounting: the request never reached a
    // conclusion about the server, so it must not count towards an outage.
    const error = new Error('aborted')
    error.name = 'AbortError'
    expect(classifyFailure(error)).toBe('request-cancelled')
  })

  // The classifier reads `name` off whatever was thrown rather than testing `instanceof Error`,
  // because a DOMException's relationship to Error differs between the browser, Node and the
  // test DOM — and a thrown value that fails the instanceof test would be classified as no
  // evidence, silently losing the outage it was reporting. These cases are the difference
  // between the two readings: real DOMExceptions pass under either one.
  it('classifies a cancellation that is not an Error instance at all', () => {
    expect(classifyFailure({ name: 'AbortError' })).toBe('request-cancelled')
  })

  it('classifies a timeout that is not an Error instance at all', () => {
    expect(classifyFailure({ name: 'TimeoutError' })).toBe('timeout')
  })

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['a string', 'Failed to fetch'],
    ['an object with no name', { message: 'something' }],
  ])('reads %s as saying nothing about the network', (_label, thrown) => {
    // Nothing here names a condition, so nothing here may move the verdict either way.
    expect(classifyFailure(thrown)).toBe('other-error')
  })

  it('reads anything else as saying nothing about the network', () => {
    expect(classifyFailure(new RangeError('bug in our own code'))).toBe('other-error')
  })
})

describe('reduceReachability — a case per category', () => {
  const fresh = initialReachabilityState()

  it('a single network error is a suspicion, not a verdict (no banner on one failure)', () => {
    const { state, effects } = reduceReachability(fresh, 'network-error', 0)
    expect(state.status).toBe('suspect')
    expect(effects).toEqual([{ type: 'probe-now' }])
  })

  it('a single timeout is a suspicion too', () => {
    expect(suspectAfter('timeout').status).toBe('suspect')
  })

  it('a 502/503/504 counts as a failure', () => {
    expect(suspectAfter('gateway-error').status).toBe('suspect')
  })

  it('a 401 never raises the banner', () => {
    const { state } = reduceReachability(fresh, 'unauthorized', 0)
    expect(state.status).toBe('reachable')
  })

  it('a 401 while unreachable proves the server is back — it answered', () => {
    const down = { status: 'unreachable' as const, failures: 4, streakStartedAt: 0 }
    const { state, effects } = reduceReachability(down, 'unauthorized', 9_000)
    expect(state.status).toBe('reachable')
    expect(effects).toContainEqual({ type: 'refetch' })
  })

  it('any other HTTP answer means reachable, even a 500', () => {
    expect(reduceReachability(fresh, 'server-answered', 0).state.status).toBe('reachable')
  })

  it('a success keeps it reachable and asks for nothing', () => {
    expect(reduceReachability(fresh, 'success', 0)).toEqual({ state: fresh, effects: [] })
  })

  it('a failed probe counts as a failure', () => {
    expect(suspectAfter('probe-failed').status).toBe('suspect')
  })

  // Driven through classifyFailure with real error objects, because the classification IS the
  // defect: feeding the reducer a literal 'request-cancelled' would prove nothing about what an
  // aborted fetch actually turns into.
  function applySequence(errors: unknown[], times: number[]) {
    let state = fresh
    errors.forEach((error, i) => {
      state = reduceReachability(state, classifyFailure(error), times[i]).state
    })
    return state
  }

  it('two requests the app cancelled itself never add up to an outage', () => {
    const cancelled = () => new DOMException('The user aborted a request.', 'AbortError')
    const state = applySequence([cancelled(), cancelled()], [0, 3_000])
    expect(state.status).toBe('reachable')
    expect(state.failures).toBe(0)
  })

  it('but two real timeouts across the same window do condemn the server', () => {
    // The contrast that proves the first case is about cancellation and not about the debounce.
    const timedOut = () => new DOMException('The operation timed out.', 'TimeoutError')
    expect(applySequence([timedOut(), timedOut()], [0, 3_000]).status).toBe('unreachable')
  })

  it('a stream error asks the probe rather than passing judgement', () => {
    // An EventSource error carries no status: it cannot tell a vanished server from a 401 after
    // the session expired, so it may only trigger the request that CAN tell them apart.
    const { state, effects } = reduceReachability(fresh, 'stream-error', 0)
    expect(state).toEqual(fresh)
    expect(effects).toEqual([{ type: 'probe-now' }])
  })

  it('an error that is neither network nor timeout changes nothing at all', () => {
    const down = { status: 'unreachable' as const, failures: 3, streakStartedAt: 0 }
    expect(reduceReachability(down, 'other-error', 5_000)).toEqual({ state: down, effects: [] })
  })

  it('the browser going online is not a verdict — it only buys an immediate probe', () => {
    const down = { status: 'unreachable' as const, failures: 3, streakStartedAt: 0 }
    const { state, effects } = reduceReachability(down, 'browser-online', 5_000)
    expect(state.status).toBe('unreachable')
    expect(effects).toEqual([{ type: 'probe-now' }])
  })

  it('"Retry now" probes immediately without changing the verdict', () => {
    const down = { status: 'unreachable' as const, failures: 3, streakStartedAt: 0 }
    const { state, effects } = reduceReachability(down, 'retry-requested', 5_000)
    expect(state.status).toBe('unreachable')
    expect(effects).toEqual([{ type: 'probe-now' }])
  })

  it('a browser online event while reachable does nothing', () => {
    expect(reduceReachability(fresh, 'browser-online', 0)).toEqual({ state: fresh, effects: [] })
  })
})

describe('reduceReachability — the debounce', () => {
  it('needs a second failure AND elapsed time before it says unreachable', () => {
    const first = reduceReachability(initialReachabilityState(), 'network-error', 0).state
    const quick = reduceReachability(first, 'probe-failed', 10).state
    expect(quick.status).toBe('suspect')

    const later = reduceReachability(quick, 'probe-failed', UNREACHABLE_AFTER_MS).state
    expect(later.status).toBe('unreachable')
  })

  it('does not flip on one failure however long ago the page loaded', () => {
    const only = reduceReachability(initialReachabilityState(), 'network-error', 60_000).state
    expect(only.status).toBe('suspect')
  })

  it('an answer in the middle of a streak resets the debounce', () => {
    const first = reduceReachability(initialReachabilityState(), 'network-error', 0).state
    const healed = reduceReachability(first, 'probe-ok', 100).state
    expect(healed).toEqual(initialReachabilityState())
    const again = reduceReachability(healed, 'network-error', 200).state
    expect(again.status).toBe('suspect')
    expect(again.failures).toBe(1)
  })
})

describe('probeDelayMs', () => {
  it('starts at one second and doubles', () => {
    expect([1, 2, 3, 4].map(probeDelayMs)).toEqual([1_000, 2_000, 4_000, 8_000])
  })

  it('caps so a long outage costs one request every 30s, not an ever-growing silence', () => {
    expect(probeDelayMs(20)).toBe(PROBE_MAX_DELAY_MS)
    expect(probeDelayMs(200)).toBe(PROBE_MAX_DELAY_MS)
  })

  it('schedules the next probe with backoff once suspect — the first wait is one second', () => {
    const first = reduceReachability(initialReachabilityState(), 'network-error', 0).state
    const { effects } = reduceReachability(first, 'probe-failed', 0)
    expect(effects).toEqual([{ type: 'probe-in', delayMs: 1_000 }])
  })

  it('never schedules a probe further out than the cap, however long the outage', () => {
    let state = reduceReachability(initialReachabilityState(), 'network-error', 0).state
    let last = 0
    for (let i = 0; i < 12; i++) {
      const step = reduceReachability(state, 'probe-failed', i * 1_000)
      state = step.state
      const effect = step.effects[0]
      last = effect.type === 'probe-in' ? effect.delayMs : last
      expect(last).toBeLessThanOrEqual(PROBE_MAX_DELAY_MS)
    }
    expect(last).toBe(PROBE_MAX_DELAY_MS)
  })
})

describe('reduceReachability — recovery', () => {
  it('stops probing and asks for a refetch when the server answers again', () => {
    const down = { status: 'unreachable' as const, failures: 5, streakStartedAt: 0 }
    const { state, effects } = reduceReachability(down, 'probe-ok', 30_000)
    expect(state).toEqual(initialReachabilityState())
    expect(effects).toEqual([{ type: 'stop-probing' }, { type: 'refetch' }])
  })

  it('does not ask for a refetch on every successful request while already reachable', () => {
    expect(reduceReachability(initialReachabilityState(), 'success', 0).effects).toEqual([])
  })
})
