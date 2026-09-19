import { onlineManager, QueryClient, QueryObserver, type Query } from '@tanstack/vue-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { recoverQueries } from '@/api/reachability'
import { createReachabilityMonitor, type ProbeResult } from '@/lib/reachabilityMonitor'

// The gap this covers is a blip the banner never showed: one failed request answers again before
// the debounce is out, so the verdict goes `reachable` -> `suspect` -> `reachable` and the online
// state never moves. Driven through the real monitor with the real recovery wiring, because the
// bug lives in the join between them, not in either one alone. Pure objects only — no component
// is mounted (happy-dom has no EventSource, and the query layer needs none).

const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

/** The monitor as `installReachability` builds it, with the clock and the probe injected. */
function wiring(client: QueryClient) {
  let now = 0
  let nextId = 1
  const pending = new Map<number, { at: number; fn: () => void }>()
  // The probe is left in flight on purpose: the test decides the exact moment the server answers
  // again, which is the moment the recovery has to happen.
  let settleProbe: ((result: ProbeResult) => void) | null = null
  const probe = vi.fn(
    () =>
      new Promise<ProbeResult>((resolve) => {
        settleProbe = resolve
      }),
  )

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
    onRecover: () => recoverQueries(client),
  })

  return {
    monitor,
    /** One failure, then the server answering: a blip that never gets past `suspect`. */
    blip: async (advanceMs = 400) => {
      monitor.report('network-error')
      await flush()
      now += advanceMs
      const resolve = settleProbe
      settleProbe = null
      resolve?.('probe-ok')
      await flush()
    },
    status: () => monitor.status(),
  }
}

/** An observed query, as a mounted view would hold it — `invalidateQueries` refetches ACTIVE ones. */
function activeQuery(client: QueryClient, key: string, queryFn: () => Promise<unknown>) {
  const observer = new QueryObserver(client, {
    queryKey: [key],
    queryFn,
    // The shape of the query this ticket names: `frontend/src/composables/useSession.ts`.
    retry: false,
    staleTime: 5 * 60 * 1000,
  })
  return { observer, unsubscribe: observer.subscribe(() => {}) }
}

describe('recovery from a blip that never raised the banner', () => {
  let client: QueryClient

  beforeEach(() => {
    client = new QueryClient()
    onlineManager.setOnline(true)
  })

  afterEach(() => {
    client.clear()
    vi.restoreAllMocks()
  })

  it('refetches a retry:false query that failed during a suspect blip', async () => {
    const queryFn = vi
      .fn<() => Promise<{ name: string }>>()
      .mockRejectedValueOnce(new Error('connection blip'))
      .mockResolvedValue({ name: 'fabian' })
    const { observer, unsubscribe } = activeQuery(client, 'session', queryFn)
    await vi.waitFor(() => expect(observer.getCurrentResult().status).toBe('error'))
    expect(queryFn).toHaveBeenCalledTimes(1)

    const reach = wiring(client)
    await reach.blip()
    // The whole point: the verdict never reached `unreachable`, so nothing was ever paused.
    expect(reach.status()).toBe('reachable')

    await vi.waitFor(() => expect(queryFn).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(observer.getCurrentResult().status).toBe('success'))
    unsubscribe()
  })

  it('leaves a query that did not fail alone', async () => {
    const healthy = vi.fn<() => Promise<{ name: string }>>().mockResolvedValue({ name: 'fabian' })
    const { observer, unsubscribe } = activeQuery(client, 'healthy', healthy)
    await vi.waitFor(() => expect(observer.getCurrentResult().status).toBe('success'))
    expect(healthy).toHaveBeenCalledTimes(1)

    await wiring(client).blip()
    await flush()
    await flush()

    // A refetch storm on every blip is the failure mode this fix must not create.
    expect(healthy).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('invalidates exactly once per recovery, and only the queries in error', async () => {
    const invalidate = vi.spyOn(client, 'invalidateQueries').mockResolvedValue(undefined)
    const reach = wiring(client)

    await reach.blip()

    expect(invalidate).toHaveBeenCalledTimes(1)
    // The filters argument is typed as "filters or a thunk returning filters"; resolve it rather
    // than casting, so the assertion below is about what TanStack actually receives.
    const argument = invalidate.mock.calls[0]?.[0]
    const filters = typeof argument === 'function' ? argument() : argument
    const predicate = filters?.predicate
    expect(predicate).toBeDefined()
    const asQuery = (status: string) => ({ state: { status } }) as unknown as Query
    expect(predicate?.(asQuery('error'))).toBe(true)
    expect(predicate?.(asQuery('success'))).toBe(false)
    expect(predicate?.(asQuery('pending'))).toBe(false)
  })

  it('counts a recovery that arrives while the previous invalidation is still running', async () => {
    // A never-settling invalidation: the second recovery must still invalidate, not be swallowed
    // by a "one at a time" guard.
    const invalidate = vi
      .spyOn(client, 'invalidateQueries')
      .mockImplementation(() => new Promise<void>(() => {}))
    const reach = wiring(client)

    await reach.blip()
    await reach.blip()

    expect(invalidate).toHaveBeenCalledTimes(2)
  })

  it('still puts the online state back, for the outage that did pause the queries', async () => {
    onlineManager.setOnline(false)
    recoverQueries(client)
    expect(onlineManager.isOnline()).toBe(true)
  })
})
