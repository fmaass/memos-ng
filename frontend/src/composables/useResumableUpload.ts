import { reactive } from 'vue'

import { redirectToLogin } from '@/api/client'

// Resumable chunked upload against /api/uploads (T2.1): create → PATCH each chunk with an
// Upload-Offset → on a chunk failure, HEAD to learn the server's authoritative offset and
// resume → finalize. Raw bytes go via plain fetch (the typed openapi-fetch client is JSON);
// same-origin so the BFF session cookie rides along. fetch is called per-request (not
// captured), so tests stub globalThis.fetch.

interface UploadCreated {
  upload_uid: string
  chunk_size: number
  bytes_received: number
}

export interface AttachmentResult {
  uid: string
  filename: string
  mime: string
  size: number
  status: string
}

const MAX_RETRIES_PER_OFFSET = 5

async function postJson<T>(url: string): Promise<T>
async function postJson<T>(url: string, body: unknown): Promise<T>
async function postJson<T>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    headers: body === undefined ? undefined : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (res.status === 401) {
    redirectToLogin()
    throw new Error('unauthorized')
  }
  if (!res.ok) throw new Error(`${url} → ${res.status}`)
  return res.json() as Promise<T>
}

async function serverOffset(uid: string): Promise<number> {
  const res = await fetch(`/api/uploads/${uid}`, { method: 'HEAD' })
  if (!res.ok) throw new Error(`HEAD ${uid} → ${res.status}`)
  return Number(res.headers.get('Upload-Offset') ?? 0)
}

export async function uploadFile(
  file: File,
  onProgress?: (pct: number) => void,
): Promise<AttachmentResult> {
  const created = await postJson<UploadCreated>('/api/uploads', {
    filename: file.name,
    mime: file.type || 'application/octet-stream',
    size: file.size,
    last_modified: file.lastModified,
  })
  const uid = created.upload_uid
  const chunkSize = created.chunk_size
  let offset = created.bytes_received
  let retries = 0

  while (offset < file.size) {
    const chunk = file.slice(offset, Math.min(offset + chunkSize, file.size))
    try {
      const res = await fetch(`/api/uploads/${uid}`, {
        method: 'PATCH',
        headers: { 'Upload-Offset': String(offset) },
        body: chunk,
      })
      if (res.status === 401) {
        redirectToLogin()
        throw new Error('unauthorized')
      }
      if (!res.ok) throw new Error(`chunk ${offset} → ${res.status}`)
      offset = (await res.json()).bytes_received
      retries = 0
      onProgress?.(Math.round((offset / file.size) * 100))
    } catch (err) {
      if (++retries > MAX_RETRIES_PER_OFFSET) throw err
      // Resync to the server's truth: if it actually received the chunk, jump forward;
      // otherwise offset is unchanged and the loop re-sends the same chunk.
      offset = await serverOffset(uid)
    }
  }

  return postJson<AttachmentResult>(`/api/uploads/${uid}/finalize`)
}

export type UploadItemStatus = 'queued' | 'uploading' | 'done' | 'error'

export interface UploadItem {
  id: number
  name: string
  progress: number
  status: UploadItemStatus
  attachmentUid?: string
  error?: string
}

// Reactive multi-file queue with bounded concurrency. Each file uploads independently so a
// memo can be posted while a video is still uploading (the queue is not awaited by save).
export function useUploadQueue(concurrency = 2) {
  const items = reactive<UploadItem[]>([])
  let nextId = 0
  let active = 0
  const pending: UploadItem[] = []

  function pump() {
    while (active < concurrency && pending.length > 0) {
      const item = pending.shift()!
      const file = files.get(item.id)!
      active++
      item.status = 'uploading'
      uploadFile(file, (pct) => (item.progress = pct))
        .then((res) => {
          item.status = 'done'
          item.progress = 100
          item.attachmentUid = res.uid
        })
        .catch((err: unknown) => {
          item.status = 'error'
          item.error = err instanceof Error ? err.message : String(err)
        })
        .finally(() => {
          active--
          pump()
        })
    }
  }

  const files = new Map<number, File>()

  function add(toAdd: File[]): UploadItem[] {
    const created = toAdd.map((file) => {
      const item: UploadItem = { id: nextId++, name: file.name, progress: 0, status: 'queued' }
      files.set(item.id, file)
      items.push(item)
      pending.push(item)
      return item
    })
    pump()
    return created
  }

  return { items, add }
}
