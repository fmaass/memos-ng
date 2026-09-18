import createClient, { type Middleware } from 'openapi-fetch'

import { classifyFailure, classifyStatus } from '@/lib/reachability'

import { reachability } from './reachability'
import type { paths } from './schema'

// A BFF session is a same-origin httpOnly cookie, so any 401 from the API means the
// session is gone (or never existed) → bounce to the OIDC login, preserving where the
// user was so the backend's `next` param lands them back after auth. Exported for tests.
export function redirectToLogin(): void {
  if (typeof window === 'undefined') return
  const path = window.location.pathname + window.location.search
  // Only forward a same-site local path; never a protocol-relative '//evil' (which the
  // browser would treat as absolute). The backend re-validates, but harden here too.
  const safe = path.startsWith('/') && !path.startsWith('//') ? path : '/'
  window.location.assign(`/api/auth/login?next=${encodeURIComponent(safe)}`)
}

// Exported for direct unit testing — openapi-fetch binds `fetch` at client creation,
// so the middleware is exercised in tests by invoking onResponse, not by stubbing fetch.
export const authMiddleware: Middleware = {
  onResponse({ response }) {
    if (response.status === 401) redirectToLogin()
    return response
  },
}

// Every API call is free evidence about whether the server is there. A fetch that never got an
// answer produces NO Response, so onResponse cannot see it — openapi-fetch surfaces it to
// onError, which is the only hook in this version that sees a network-level failure.
// Exported for direct unit testing, same reason as authMiddleware above.
export const reachabilityMiddleware: Middleware = {
  onResponse({ response }) {
    reachability.report(classifyStatus(response.status))
    return response
  },
  onError({ error }) {
    reachability.report(classifyFailure(error))
    // Returning nothing leaves openapi-fetch to rethrow the original error untouched, so every
    // caller's error handling behaves exactly as it did before this hook existed.
  },
}

// Single typed client over the generated OpenAPI schema. No hand-written request/
// response types anywhere (ADR-0009). Same-origin: the SPA is served by FastAPI.
export const api = createClient<paths>({ baseUrl: '/' })
api.use(authMiddleware)
api.use(reachabilityMiddleware)
