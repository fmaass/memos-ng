import createClient from 'openapi-fetch'
import { vi } from 'vitest'

import type { paths } from '@/api/schema'
import type { PassDevice, PassOut } from '@/lib/tripPasses'

/** One request as the wire saw it. */
export interface Seen {
  method: string
  path: string
  body: unknown
}

type Answer = { status: number; body: unknown } | Promise<{ status: number; body: unknown }>
type Handler = (req: Seen) => Answer

/**
 * The HTTP boundary and nothing else: the REAL generated openapi-fetch client, handed a fetch
 * that answers from a route table. Everything above the wire — the composable, the query cache,
 * the components — is the code under test.
 */
export function passServer() {
  const seen: Seen[] = []
  const routes = new Map<string, Handler>()
  const fetchImpl = vi.fn(async (input: Request) => {
    const url = new URL(input.url)
    const text = await input.text()
    const req: Seen = {
      method: input.method,
      path: url.pathname + url.search,
      body: text ? JSON.parse(text) : undefined,
    }
    seen.push(req)
    const handler = routes.get(`${req.method} ${req.path}`)
    const answer = handler ? await handler(req) : { status: 599, body: { detail: 'unrouted' } }
    return new Response(answer.body === undefined ? null : JSON.stringify(answer.body), {
      status: answer.status,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  const client = createClient<paths>({ baseUrl: 'http://test', fetch: fetchImpl })
  return {
    client,
    seen,
    on(method: string, path: string, handler: Handler | { status: number; body: unknown }) {
      routes.set(`${method} ${path}`, typeof handler === 'function' ? handler : () => handler)
    },
    calls: (method: string, path: string) =>
      seen.filter((r) => r.method === method && r.path === path),
  }
}

export function aPass(over: Partial<PassOut> = {}): PassOut {
  return {
    uid: 'pass1',
    collectionUid: 'trip1',
    label: 'Grandparents',
    scope: 'contribute',
    state: 'active',
    expiresAt: '2026-11-20T22:59:59Z',
    maxDevices: 30,
    notes: null,
    version: 1,
    revokedAt: null,
    revokedVersion: null,
    deviceCount: 0,
    createdBy: 'fabian',
    createdAt: '2026-10-28T08:00:00Z',
    updatedAt: '2026-10-28T08:00:00Z',
    ...over,
  }
}

export function aDevice(over: Partial<PassDevice> = {}): PassDevice {
  return {
    uid: 'dev1',
    displayName: 'Oma',
    platform: 'web',
    firstSeen: '2026-10-28T09:00:00Z',
    lastSeen: '2026-10-28T09:30:00Z',
    uploadsCount: 4,
    bytesTotal: 8_000_000,
    revoked: false,
    revokedAt: null,
    revokedVersion: null,
    ...over,
  }
}

/** A QR exactly as app/services/passes.py:qr_svg renders it (segno, scale 4, border 4), trimmed to
 * two path runs. */
export const SERVER_QR_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="148" height="148">' +
  '<path transform="scale(4)" stroke="#000" d="M4 4.5h7m1 0h1m2 0h1m1 0h3"/></svg>'
