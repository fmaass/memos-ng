import { createHash } from 'node:crypto'
import type { FetchLike } from '../api'

// An in-memory stand-in for the door's TRANSPORT (app/ingest/guest.py semantics: offsets, replay
// no-ops, completion keys, discard on 415/422), so the real api client and the real uploader are
// what runs. Every request is logged as the wire saw it.

export interface Wire {
  method: string
  path: string
  token: string | null
  offset?: number
  bytes?: number
  uids?: string[]
  status: number
  referrer?: string
}

interface Upload {
  uid: string
  filename: string
  size: number
  sha256: string | null
  data: Uint8Array
}

export class FakeDoor {
  readonly wire: Wire[] = []
  readonly uploads = new Map<string, Upload>()
  readonly batches = new Map<string, { batch_id: string; uids: string[] }>()
  readonly received = new Set<string>()
  tokens = new Set(['trip_dev_one'])
  limits = { max_file_bytes: 1 << 30, chunk_size: 4096, max_patch_bytes: 4096 }
  /** Corrupt the stored bytes of uploads whose filename/size match, so completion answers 422. */
  damage: (u: Upload) => boolean = () => false
  /** Bytes the server keeps of the next PATCH before the connection drops (a tab kill). */
  cutNextPatchAt: number | null = null
  /** Every PATCH keeps all its bytes and loses its answer (a connection that drops on each reply). */
  cutEveryPatch = false
  /** Answer completion like the server, then lose the response on the wire. */
  loseNextCompletionResponse = false
  unsupported: (u: Upload) => boolean = () => false
  /** Hold PATCH answers until released (for fence tests). */
  gate: Promise<void> | null = null
  patchStatus: number | null = null
  completeHang: Promise<void> | null = null
  private n = 0

  fetch: FetchLike = async (input, init) => {
    const method = init.method ?? 'GET'
    const headers = new Headers(init.headers)
    const auth = headers.get('Authorization')
    const token = auth?.startsWith('Bearer ') ? auth.slice(7) : null
    const entry: Wire = { method, path: input, token, status: 0, referrer: String(init.referrerPolicy) }
    this.wire.push(entry)
    const answer = (status: number, body: unknown = null, extra: Record<string, string> = {}) => {
      entry.status = status
      const text = body === null ? null : JSON.stringify(body)
      return new Response(method === 'HEAD' ? null : text, {
        status,
        headers: { 'Content-Type': 'application/json', ...extra },
      })
    }
    if (input === '/api/pass/redeem') return answer(201, { device_token: 'trip_dev_one', device_id: 'dev1', pass: { label: 'Trip', scope: 'x', expires_at: '' }, limits: this.limits })
    if (!token || !this.tokens.has(token)) return answer(401, { detail: 'Not a valid guest credential' })
    if (input === '/api/guest/uploads' && method === 'POST') {
      const body = JSON.parse(String(init.body))
      const ext = String(body.filename).split('.').pop()!.toLowerCase()
      if (!['jpg', 'jpeg', 'png', 'heic', 'mov', 'mp4'].includes(ext)) return answer(415, { detail: 'This file type is not accepted' })
      if (body.sha256 && this.received.has(body.sha256)) return answer(200, { already_received: true, upload_uid: null, bytes_received: 0, chunk_size: 0, max_patch_bytes: 0 })
      const uid = `u${String(++this.n).padStart(3, '0')}`
      this.uploads.set(uid, { uid, filename: body.filename, size: body.size, sha256: body.sha256, data: new Uint8Array(0) })
      return answer(201, { already_received: false, upload_uid: uid, bytes_received: 0, chunk_size: this.limits.chunk_size, max_patch_bytes: this.limits.max_patch_bytes })
    }
    const m = /^\/api\/guest\/uploads\/(.+)$/.exec(input)
    if (m) {
      const upload = this.uploads.get(decodeURIComponent(m[1]))
      if (method === 'HEAD') {
        if (!upload) return answer(404)
        return answer(200, null, { 'Upload-Offset': String(upload.data.length) })
      }
      const offset = Number(headers.get('Upload-Offset'))
      const chunk = new Uint8Array(await (init.body as Blob).arrayBuffer())
      entry.offset = offset
      entry.bytes = chunk.length
      if (this.gate) await this.gate
      if (this.patchStatus) return answer(this.patchStatus, { detail: 'refused' })
      if (!upload) return answer(404, { detail: 'Upload not found' })
      if (offset > upload.data.length) return answer(409, { detail: 'Offset gap; re-sync via HEAD' })
      if (offset < upload.data.length) return answer(200, {}, { 'Upload-Offset': String(upload.data.length) })
      let kept = chunk
      const cut = this.cutEveryPatch ? chunk.length : this.cutNextPatchAt
      if (cut !== null) kept = chunk.slice(0, cut)
      const merged: Uint8Array = new Uint8Array(upload.data.length + kept.length)
      merged.set(upload.data)
      merged.set(kept, upload.data.length)
      upload.data = merged
      if (cut !== null) {
        this.cutNextPatchAt = null
        entry.status = -1
        throw new TypeError('network connection was lost')
      }
      return answer(200, {}, { 'Upload-Offset': String(upload.data.length) })
    }
    if (input === '/api/guest/memos') {
      const body = JSON.parse(String(init.body))
      const uids: string[] = [...new Set<string>(body.upload_uids)].sort()
      entry.uids = uids
      if (this.completeHang) await this.completeHang
      const key = uids.join(',')
      const existing = this.batches.get(key)
      if (existing) return answer(200, this.receipt(existing.batch_id, uids.length))
      const rows = uids.map((u) => this.uploads.get(u))
      if (rows.some((r) => !r)) return answer(409, { detail: 'An upload is unknown or already completed' })
      if (rows.some((r) => r!.data.length !== r!.size)) return answer(409, { detail: 'An upload is not fully received' })
      for (const r of rows as Upload[]) {
        const sha = createHash('sha256').update(r.data).digest('hex')
        const failed = this.unsupported(r) ? 415 : this.damage(r) || (r.sha256 && r.sha256 !== sha) ? 422 : 0
        if (failed) {
          this.uploads.delete(r.uid) // discarded and committed, as the server does
          if (this.loseNextCompletionResponse) {
            this.loseNextCompletionResponse = false
            entry.status = -1
            throw new TypeError('network connection was lost')
          }
          const detail = failed === 415 ? `${r.filename} is not the picture or video its name says` : `${r.filename} arrived damaged`
          return answer(failed, { detail })
        }
      }
      const batch_id = `b${this.batches.size + 1}`
      this.batches.set(key, { batch_id, uids })
      for (const r of rows as Upload[]) {
        this.received.add(createHash('sha256').update(r.data).digest('hex'))
        this.uploads.delete(r.uid)
      }
      if (this.loseNextCompletionResponse) {
        this.loseNextCompletionResponse = false
        entry.status = -1
        throw new TypeError('network connection was lost')
      }
      return answer(200, this.receipt(batch_id, uids.length))
    }
    if (input === '/api/guest/me') return answer(200, { device_id: 'dev1', display_name: 'Anna', platform: 'web', pass: { label: 'Trip', scope: 'x', expires_at: '' }, limits: this.limits, uploads_count: 0, bytes_total: 0 })
    return answer(404, { detail: 'Not found' })
  }

  receipt(batch_id: string, n: number) {
    return { batch_id, status: 'received', file_count: n, byte_total: 0, caption: null, completed_at: null, files: [] }
  }

  calls(method: string, prefix: string): Wire[] {
    return this.wire.filter((w) => w.method === method && w.path.startsWith(prefix))
  }
}

export function bytes(n: number, seed: number): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(n)
  for (let i = 0; i < n; i++) out[i] = (i * 31 + seed * 17 + (i >> 8)) & 0xff
  return out
}

export function fileOf(data: Uint8Array, name: string, lastModified = 1_700_000_000_000): File {
  return new File([data.slice()], name, { type: 'image/jpeg', lastModified })
}

export const sha = (d: Uint8Array) => createHash('sha256').update(d).digest('hex')
