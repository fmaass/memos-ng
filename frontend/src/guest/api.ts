// The guest door's HTTP contract (app/ingest/guest.py). Every request is credential-less at the
// browser level, uncached and sends no Referer: the bearer token is the only identity.

export interface Limits {
  max_file_bytes: number
  chunk_size: number
  max_patch_bytes: number
}
export interface PassInfo {
  label: string
  scope: string
  expires_at: string
}
export interface RedeemOut {
  device_token: string
  device_id: string
  pass: PassInfo
  limits: Limits
}
export interface MeOut {
  device_id: string
  display_name: string
  platform: string
  pass: PassInfo
  limits: Limits
  uploads_count: number
  bytes_total: number
}
export interface CreateIn {
  filename: string
  mime: string
  size: number
  last_modified: number | null
  sha256: string
}
export interface CreateOut {
  already_received: boolean
  upload_uid: string | null
  bytes_received: number
  chunk_size: number
  max_patch_bytes: number
}
export interface Receipt {
  batch_id: string
  status: 'received' | 'held'
  file_count: number
  byte_total: number
  caption: string | null
  completed_at: string | null
  files: { filename: string; sha256: string; size: number }[]
}

export class GuestHttpError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
    readonly retryAfter: number | null = null,
  ) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`)
  }
}

const BASE: RequestInit = {
  credentials: 'omit',
  cache: 'no-store',
  referrerPolicy: 'no-referrer',
  redirect: 'error',
}

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>

export function createGuestApi(fetchImpl: FetchLike = (input, init) => fetch(input, init)) {
  async function call(path: string, init: RequestInit, token?: string): Promise<Response> {
    const headers = new Headers(init.headers)
    if (token) headers.set('Authorization', `Bearer ${token}`)
    const response = await fetchImpl(path, { ...BASE, ...init, headers })
    if (!response.ok) {
      let detail: unknown = null
      try {
        detail = ((await response.json()) as { detail?: unknown }).detail ?? null
      } catch {
        detail = null // a HEAD or a non-JSON refusal carries no detail; the status decides
      }
      const retry = Number(response.headers.get('Retry-After'))
      throw new GuestHttpError(response.status, detail, Number.isFinite(retry) && retry > 0 ? retry : null)
    }
    return response
  }
  const json = { 'Content-Type': 'application/json' }
  return {
    async redeem(secret: string, displayName: string): Promise<RedeemOut> {
      const body = JSON.stringify({ secret, display_name: displayName, platform: 'web' })
      return (await call('/api/pass/redeem', { method: 'POST', headers: json, body })).json()
    },
    async me(token: string): Promise<MeOut> {
      return (await call('/api/guest/me', { method: 'GET' }, token)).json()
    },
    async create(token: string, body: CreateIn): Promise<CreateOut> {
      const init = { method: 'POST', headers: json, body: JSON.stringify(body) }
      return (await call('/api/guest/uploads', init, token)).json()
    },
    /** The committed offset, or null when the server holds no such upload (404). */
    async head(token: string, uid: string): Promise<number | null> {
      try {
        const r = await call(`/api/guest/uploads/${encodeURIComponent(uid)}`, { method: 'HEAD' }, token)
        return Number(r.headers.get('Upload-Offset'))
      } catch (error) {
        if (error instanceof GuestHttpError && error.status === 404) return null
        throw error
      }
    },
    /** Appends `body` at `offset`; answers the server's authoritative offset afterwards. */
    async patch(token: string, uid: string, offset: number, body: Blob, signal?: AbortSignal): Promise<number> {
      const init: RequestInit = {
        method: 'PATCH',
        headers: { 'Upload-Offset': String(offset), 'Content-Type': 'application/offset+octet-stream' },
        body,
        signal,
      }
      const r = await call(`/api/guest/uploads/${encodeURIComponent(uid)}`, init, token)
      return Number(r.headers.get('Upload-Offset'))
    },
    async complete(token: string, uids: string[], caption: string | null): Promise<Receipt> {
      const body = JSON.stringify({ upload_uids: uids, caption })
      return (await call('/api/guest/memos', { method: 'POST', headers: json, body }, token)).json()
    },
  }
}

export type GuestApi = ReturnType<typeof createGuestApi>
