import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { reachability } from '@/api/reachability'
import { reachabilityMiddleware } from '@/api/client'

// openapi-fetch binds fetch at client creation, so the hooks are exercised directly — the same
// approach the comment above authMiddleware prescribes.
function runOnResponse(status: number) {
  return reachabilityMiddleware.onResponse!({
    response: new Response(null, { status }),
  } as never)
}

function runOnError(error: unknown) {
  return reachabilityMiddleware.onError!({ error } as never)
}

describe('reachabilityMiddleware', () => {
  let report: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    // Spy at the decision point: what the middleware TELLS the monitor, not what the monitor
    // later concludes — a state read would pass even if the hook reported the wrong category.
    report = vi.spyOn(reachability, 'report').mockImplementation(() => {})
  })
  afterEach(() => vi.restoreAllMocks())

  it('reports a failed fetch (TypeError) as a network error', () => {
    runOnError(new TypeError('Failed to fetch'))
    expect(report).toHaveBeenCalledWith('network-error')
  })

  it('reports an aborted/timed-out request as a timeout', () => {
    const error = new Error('aborted')
    error.name = 'TimeoutError'
    runOnError(error)
    expect(report).toHaveBeenCalledWith('timeout')
  })

  it('leaves the original error to openapi-fetch — returning nothing means it is rethrown', () => {
    expect(runOnError(new TypeError('Failed to fetch'))).toBeUndefined()
  })

  it.each([502, 503, 504])('reports a %d as a gateway error', (status) => {
    runOnResponse(status)
    expect(report).toHaveBeenCalledWith('gateway-error')
  })

  it('reports a 401 as an auth outcome, not a connectivity one', () => {
    runOnResponse(401)
    expect(report).toHaveBeenCalledWith('unauthorized')
  })

  it.each([200, 404, 500])('reports a %d as the server having answered', (status) => {
    runOnResponse(status)
    expect(report).toHaveBeenCalledWith('server-answered')
  })

  it('passes the response through unchanged', () => {
    const response = new Response(null, { status: 200 })
    const returned = reachabilityMiddleware.onResponse!({ response } as never)
    expect(returned).toBe(response)
  })
})

describe('the auth middleware is untouched by reachability', () => {
  afterEach(() => vi.restoreAllMocks())

  it('a 401 still redirects to login exactly once, through its own middleware', async () => {
    vi.spyOn(reachability, 'report').mockImplementation(() => {})
    const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    const { authMiddleware } = await import('@/api/client')
    authMiddleware.onResponse!({ response: new Response(null, { status: 401 }) } as never)
    expect(assign).toHaveBeenCalledTimes(1)
  })
})
