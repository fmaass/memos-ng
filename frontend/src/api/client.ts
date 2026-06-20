import createClient, { type Middleware } from 'openapi-fetch'

import type { paths } from './schema'

// A BFF session is a same-origin httpOnly cookie, so any 401 from the API means the
// session is gone (or never existed) → bounce to the OIDC login, preserving where the
// user was so the backend's `next` param lands them back after auth. Exported for tests.
export function redirectToLogin(): void {
  if (typeof window === 'undefined') return
  const next = encodeURIComponent(window.location.pathname + window.location.search)
  window.location.assign(`/api/auth/login?next=${next}`)
}

// Exported for direct unit testing — openapi-fetch binds `fetch` at client creation,
// so the middleware is exercised in tests by invoking onResponse, not by stubbing fetch.
export const authMiddleware: Middleware = {
  onResponse({ response }) {
    if (response.status === 401) redirectToLogin()
    return response
  },
}

// Single typed client over the generated OpenAPI schema. No hand-written request/
// response types anywhere (ADR-0009). Same-origin: the SPA is served by FastAPI.
export const api = createClient<paths>({ baseUrl: '/' })
api.use(authMiddleware)
