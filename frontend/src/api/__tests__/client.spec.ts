import { afterEach, describe, expect, it, vi } from 'vitest'

import { authMiddleware, redirectToLogin } from '@/api/client'

// openapi-fetch's onResponse passes an options object; only `response` matters here.
function runOnResponse(status: number) {
  return authMiddleware.onResponse!({
    response: new Response(null, { status }),
  } as never)
}

describe('redirectToLogin', () => {
  afterEach(() => vi.restoreAllMocks())

  it('sends the browser to OIDC login, preserving the current path as next', () => {
    const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    window.history.pushState({}, '', '/archived?q=x')
    redirectToLogin()
    expect(assign).toHaveBeenCalledWith(
      `/api/auth/login?next=${encodeURIComponent('/archived?q=x')}`,
    )
  })

  it('does not forward a protocol-relative path as next (open-redirect guard)', () => {
    const assign = vi.fn()
    const original = window.location
    // happy-dom blocks cross-origin pushState, so inject a '//evil' pathname directly.
    Object.defineProperty(window, 'location', {
      value: { pathname: '//evil.com', search: '', assign },
      configurable: true,
    })
    redirectToLogin()
    expect(assign).toHaveBeenCalledWith(`/api/auth/login?next=${encodeURIComponent('/')}`)
    Object.defineProperty(window, 'location', { value: original, configurable: true })
  })
})

describe('authMiddleware', () => {
  afterEach(() => vi.restoreAllMocks())

  it('redirects to login on a 401 response', () => {
    const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    runOnResponse(401)
    expect(assign).toHaveBeenCalledWith(expect.stringContaining('/api/auth/login?next='))
  })

  it('does not redirect on a successful response', () => {
    const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    runOnResponse(200)
    expect(assign).not.toHaveBeenCalled()
  })
})
