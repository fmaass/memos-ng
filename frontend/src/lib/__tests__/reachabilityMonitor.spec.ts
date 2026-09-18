import { describe, expect, it, vi } from 'vitest'

import { PROBE_MAX_DELAY_MS, UNREACHABLE_AFTER_MS } from '@/lib/reachability'
import { createReachabilityMonitor, type ProbeResult } from '@/lib/reachabilityMonitor'

// A fake clock and a fake timer queue: the debounce and the backoff are measured, never slept
// through. Timers fire only when the test advances the clock past their due time.
function harness(probeResults: ProbeResult[] = []) {
  let now = 0
  let nextId = 1
  const pending = new Map<number, { at: number; fn: () => void }>()
  const probe = vi.fn(async () => probeResults.shift() ?? 'probe-failed')
  const onRecover = vi.fn()

  const monitor = createReachabilityMonitor({
    now: () => now,
    setTimer: (fn, delayMs) => {
      const id = nextId++
      pending.set(id, { at: now + delayMs, fn })
      return id
    },
    clearTimer: (handle) => {
      pending.delete(handle as number)
    },
    probe,
    onRecover,
  })

  async function advance(ms: number) {
    now += ms
    for (const [id, timer] of [...pending]) {
      if (timer.at > now) continue
      pending.delete(id)
      timer.fn()
    }
    // Let the probe promise chain settle before the test asserts on the result.
    await flush()
  }

  return {
    monitor,
    probe,
    onRecover,
    advance,
    scheduled: () => [...pending.values()].map((t) => t.at - now),
    setNow: (value: number) => {
      now = value
    },
  }
}

const flush = async () => {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

describe('createReachabilityMonitor', () => {
  it('starts reachable and probes nothing', () => {
    const h = harness()
    expect(h.monitor.status()).toBe('reachable')
    expect(h.probe).not.toHaveBeenCalled()
  })

  it('probes immediately on the first failure but does not raise the verdict yet', async () => {
    const h = harness(['probe-failed'])
    h.monitor.report('network-error')
    expect(h.probe).toHaveBeenCalledTimes(1)
    await flush()
    expect(h.monitor.status()).toBe('suspect')
  })

  it('reaches unreachable only once the failures have outlasted the debounce window', async () => {
    const h = harness(['probe-failed', 'probe-failed', 'probe-failed', 'probe-failed'])
    h.monitor.report('network-error')
    await flush()
    expect(h.monitor.status()).toBe('suspect')

    // Walk the backoff: 1s, then 2s, which crosses the 3s window.
    await h.advance(1_000)
    await h.advance(2_000)
    expect(h.monitor.snapshot().streakStartedAt).toBe(0)
    expect(UNREACHABLE_AFTER_MS).toBe(3_000)
    expect(h.monitor.status()).toBe('unreachable')
  })

  it('caps the backoff so a long outage settles at one probe every 30 seconds', async () => {
    const h = harness(Array.from({ length: 12 }, () => 'probe-failed' as ProbeResult))
    h.monitor.report('network-error')
    await flush()
    for (const delay of [1_000, 2_000, 4_000, 8_000, 16_000, 30_000]) {
      expect(h.scheduled()).toEqual([delay])
      await h.advance(delay)
    }
    expect(h.scheduled()).toEqual([PROBE_MAX_DELAY_MS])
  })

  it('clears on its own when the probe finally answers, and asks for a refetch', async () => {
    const h = harness(['probe-failed', 'probe-failed', 'probe-failed', 'probe-ok'])
    h.monitor.report('network-error')
    await flush()
    await h.advance(1_000)
    await h.advance(2_000)
    expect(h.monitor.status()).toBe('unreachable')

    const seen: string[] = []
    h.monitor.subscribe((status) => seen.push(status))
    await h.advance(4_000)
    expect(h.monitor.status()).toBe('reachable')
    expect(seen).toEqual(['reachable'])
    expect(h.onRecover).toHaveBeenCalledTimes(1)
    expect(h.scheduled()).toEqual([])
  })

  it('a 401 arriving while unreachable clears the banner instead of feeding it', async () => {
    const h = harness(['probe-failed', 'probe-failed'])
    h.monitor.report('network-error')
    await flush()
    await h.advance(1_000)
    await h.advance(2_000)
    expect(h.monitor.status()).toBe('unreachable')

    h.monitor.report('unauthorized')
    expect(h.monitor.status()).toBe('reachable')
  })

  it('"Retry now" probes at once instead of waiting out the backoff', async () => {
    const h = harness(['probe-failed', 'probe-failed', 'probe-failed', 'probe-ok'])
    h.monitor.report('network-error')
    await flush()
    await h.advance(1_000)
    await h.advance(2_000)
    expect(h.monitor.status()).toBe('unreachable')

    const before = h.probe.mock.calls.length
    h.monitor.retryNow()
    await flush()
    expect(h.probe.mock.calls.length).toBe(before + 1)
    expect(h.monitor.status()).toBe('reachable')
  })

  it('never runs two probes at once', async () => {
    let release: (value: ProbeResult) => void = () => {}
    const probe = vi.fn(() => new Promise<ProbeResult>((resolve) => (release = resolve)))
    const monitor = createReachabilityMonitor({
      now: () => 0,
      setTimer: () => 1,
      clearTimer: () => {},
      probe,
    })
    monitor.report('network-error')
    monitor.report('timeout')
    monitor.retryNow()
    expect(probe).toHaveBeenCalledTimes(1)
    release('probe-failed')
    await flush()
  })

  it('stops probing while the tab is hidden and picks it straight back up', async () => {
    const h = harness(['probe-failed', 'probe-failed'])
    h.monitor.report('network-error')
    await flush()
    expect(h.scheduled()).toEqual([1_000])

    h.monitor.pause()
    expect(h.scheduled()).toEqual([])
    const whileHidden = h.probe.mock.calls.length
    await h.advance(60_000)
    expect(h.probe.mock.calls.length).toBe(whileHidden)

    h.monitor.resume()
    await flush()
    expect(h.probe.mock.calls.length).toBe(whileHidden + 1)
  })

  it('notifies subscribers only when the verdict actually changes', async () => {
    const h = harness(['probe-failed', 'probe-failed', 'probe-failed'])
    const seen: string[] = []
    h.monitor.subscribe((status) => seen.push(status))
    h.monitor.report('network-error')
    await flush()
    await h.advance(1_000)
    await h.advance(2_000)
    expect(seen).toEqual(['suspect', 'unreachable'])
  })
})
