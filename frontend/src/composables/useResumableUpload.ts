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

// Upload the file's bytes and return its upload_uid. Finalize-on-post: the memo-create call
// turns the upload into an attachment, so an abandoned compose leaves only a (self-expiring)
// Upload and never an orphan attachment.
export async function uploadFile(file: File, onProgress?: (pct: number) => void): Promise<string> {
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

  return uid // NOT finalized here — the memo create finalizes it (finalize-on-post)
}

export type UploadItemStatus = 'queued' | 'uploading' | 'done' | 'error'

export interface UploadItem {
  id: number
  name: string
  progress: number
  status: UploadItemStatus
  uploadUid?: string // the pending upload; finalized into an attachment when the memo posts
  error?: string
  preview?: string // local object URL for an instant image thumbnail while uploading
  isVideo?: boolean
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
        .then((uploadUid) => {
          item.status = 'done'
          item.progress = 100
          item.uploadUid = uploadUid
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
      const isImage = file.type.startsWith('image/')
      const item: UploadItem = {
        id: nextId++,
        name: file.name,
        progress: 0,
        status: 'queued',
        preview: isImage && typeof URL.createObjectURL === 'function'
          ? URL.createObjectURL(file)
          : undefined,
        isVideo: file.type.startsWith('video/'),
      }
      files.set(item.id, file)
      items.push(item)
      pending.push(item)
      return item
    })
    pump()
    return created
  }

  // Drop a tile from the queue. A still-uploading file keeps uploading server-side but, since it's
  // no longer in the queue, its upload_uid won't be posted → it expires (finalize-on-post). Frees
  // the object-URL preview.
  function remove(id: number): void {
    const i = items.findIndex((it) => it.id === id)
    if (i < 0) return
    const [it] = items.splice(i, 1)
    if (it.preview && typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(it.preview)
    files.delete(id)
  }

  return { items, add, remove }
}
