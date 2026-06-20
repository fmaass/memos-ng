import createClient from 'openapi-fetch'

import type { paths } from './schema'

// Single typed client over the generated OpenAPI schema. No hand-written request/
// response types anywhere (ADR-0009). Same-origin: the SPA is served by FastAPI.
export const api = createClient<paths>({ baseUrl: '/' })
