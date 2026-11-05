import { getCurrentScope, onScopeDispose, reactive, shallowReactive, watch } from 'vue'

import { redirectToLogin } from '@/api/client'
import { hasActiveUpload } from '@/lib/sseLifecycle'
import { applyUploadProgress, type UploadProgressEvent } from '@/lib/uploadProgress'

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
export async function uploadFile(
  file: File,
  onProgress?: (pct: number) => void,
  onCreated?: (uploadUid: string) => void,
): Promise<string> {
  const created = await postJson<UploadCreated>('/api/uploads', {
    filename: file.name,
    mime: file.type || 'application/octet-stream',
    size: file.size,
    last_modified: file.lastModified,
  })
  const uid = created.upload_uid
  onCreated?.(uid)
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
export type CaptureDecision = 'pending' | 'done'

export interface UploadItem {
  id: number
  name: string
  progress: number
  status: UploadItemStatus
  uploadUid?: string // the pending upload; finalized into an attachment when the memo posts
  error?: string
  preview?: string // local object URL for an instant image thumbnail while uploading
  isVideo?: boolean
  // What the SERVER read out of this file's own metadata (MEMNG-262). `pending` until the
  // inspection answers; `done` is settled even when all three values are null — a file with no
  // capture date is an answer. The composer's Auto date and its tile order both wait for it.
  captureState: CaptureDecision
  captureInstant: string | null
  captureCivil: string | null
  captureSource: string | null
  // The inspection gave up (it kept answering `pending`, or the network did not answer at all).
  // The item settles as UNDATED so the composer is never blocked for ever — and says so on the
  // tile, because a date silently missing from a memo is exactly what this feature is fixing.
  captureUnknown: boolean
}

// The inspection poll: the server reads the file the moment its last chunk lands, so the first
// look almost always answers `done`. The backoff exists for the file that is slower than that,
// and it is BOUNDED — an item that never settles is treated as one with no capture date rather
// than blocking the composer's Save for ever.
const CAPTURE_POLL_DELAYS_MS = [0, 150, 300, 600, 1000, 1500, 2000, 3000, 3000, 5000, 5000]
// Per REQUEST, not per poll: without it one socket that never answers holds the whole bounded
// loop open, and "bounded" becomes a promise the code does not keep.
const CAPTURE_REQUEST_TIMEOUT_MS = 4000

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

// Every queue a live component owns, so a frame of the event stream can reach its tiles without the
// stream knowing which composer or editor is open. Registered only inside an effect scope, whose
// teardown removes it: a queue built outside one has no teardown to leave the set by. Reactive, so
// a queue arriving or leaving is a change the event stream's lifetime is re-decided on (MEMNG-1181).
const liveQueues = shallowReactive(new Set<UploadItem[]>())

// An `upload.progress` frame (MEMNG-965) applied to the tile uploading those bytes, wherever it is.
// The mapping is the pure applyUploadProgress; this only finds the tiles.
export function applyUploadProgressEvent(ev: UploadProgressEvent): void {
  for (const items of liveQueues) {
    for (const item of items) {
      const next = applyUploadProgress(item, ev)
      if (next !== null) item.progress = next
    }
  }
}

// What the event stream needs to know about the upload queues to decide its own lifetime
// (MEMNG-1181): whether any live queue is still sending bytes, and a callback for when that
// answer changes. Injected into startMediaEvents so its tests drive it without a real queue.
export interface UploadActivity {
  active: () => boolean
  subscribe: (onChange: () => void) => () => void
}

function anyLiveUploadActive(): boolean {
  for (const items of liveQueues) {
    if (hasActiveUpload(items.map((item) => item.status))) return true
  }
  return false
}

export const liveUploadActivity: UploadActivity = {
  active: anyLiveUploadActive,
  // `sync`: the stream opens in the same tick the first file starts, not after a render, so the
  // upload's first progress frame is not sent into a connection that does not exist yet.
  subscribe: (onChange) => watch(anyLiveUploadActive, () => onChange(), { flush: 'sync' }),
}

// Reactive multi-file queue with bounded concurrency. Each file uploads independently so a
// memo can be posted while a video is still uploading (the queue is not awaited by save).
export function useUploadQueue(concurrency = 2) {
  const items = reactive<UploadItem[]>([])
  let nextId = 0
  let active = 0
  const pending: UploadItem[] = []
  // One abort handle per tile, so removing a tile stops its inspection poll at once.
  const captureAborts = new Map<number, AbortController>()

  /**
   * Ask the server what this upload's file says about its capture date, until it answers.
   *
   * IDENTITY-FENCED: the answer is applied only if the tile that ISSUED the request is still in
   * the queue AND still holds the same upload_uid. A response that outlives its tile — removed,
   * retried into a new upload — is dropped, so it can never mark a DIFFERENT file done or date a
   * memo from media that is no longer attached.
   */
  async function pollCapture(id: number, uid: string): Promise<void> {
    const controller = new AbortController()
    captureAborts.get(id)?.abort()
    captureAborts.set(id, controller)
    const live = () => {
      const it = items.find((i) => i.id === id)
      return it && it.uploadUid === uid && !controller.signal.aborted ? it : undefined
    }
    try {
      for (const delay of CAPTURE_POLL_DELAYS_MS) {
        if (delay) await wait(delay)
        if (!live()) return
        const request = new AbortController()
        const abortRequest = () => request.abort()
        const timer = setTimeout(abortRequest, CAPTURE_REQUEST_TIMEOUT_MS)
        controller.signal.addEventListener('abort', abortRequest)
        try {
          const res = await fetch(`/api/uploads/${uid}/capture`, { signal: request.signal })
          if (!res.ok) break // 404 = the session is gone; nothing more to learn here
          const body = (await res.json()) as {
            state: CaptureDecision
            capture_instant: string | null
            capture_civil: string | null
            capture_source: string | null
          }
          const item = live() // re-checked AFTER the await: the tile may have gone meanwhile
          if (!item) return
          if (body.state === 'done') {
            item.captureInstant = body.capture_instant
            item.captureCivil = body.capture_civil
            item.captureSource = body.capture_source
            item.captureState = 'done'
            return
          }
        } catch {
          if (controller.signal.aborted) return
          break // a transport failure (or this request's own timeout) ends the poll
        } finally {
          clearTimeout(timer)
          controller.signal.removeEventListener('abort', abortRequest)
        }
      }
      const item = live()
      if (item) {
        // Settled with no date rather than pending for ever — and MARKED, so the tile can say the
        // date is unknown instead of the memo quietly taking today's.
        item.captureState = 'done'
        item.captureUnknown = true
      }
    } finally {
      if (captureAborts.get(id) === controller) captureAborts.delete(id)
    }
  }

  function pump() {
    while (active < concurrency && pending.length > 0) {
      const item = pending.shift()!
      const file = files.get(item.id)!
      active++
      item.status = 'uploading'
      uploadFile(
        file,
        (pct) => (item.progress = pct),
        // Known from the create on, so a progress frame can find this tile while it uploads.
        // Nothing posts it before `done`: every submitter filters on status first.
        (uploadUid) => (item.uploadUid = uploadUid),
      )
        .then((uploadUid) => {
          item.status = 'done'
          item.progress = 100
          item.uploadUid = uploadUid
          void pollCapture(item.id, uploadUid)
        })
        .catch((err: unknown) => {
          item.status = 'error'
          item.error = err instanceof Error ? err.message : String(err)
          // A file whose bytes never landed has no capture decision to wait for — and it is not
          // posted either (only `done` tiles contribute an upload_uid), so it settles here rather
          // than holding the composer. Not `captureUnknown`: the tile already shows the upload
          // error, and a second complaint about the date of a file that is not going anywhere
          // would only add noise.
          item.captureState = 'done'
        })
        .finally(() => {
          active--
          pump()
        })
    }
  }

  const files = new Map<number, File>()

  function add(toAdd: File[]): UploadItem[] {
    const created: UploadItem[] = []
    for (const file of toAdd) {
      const isImage = file.type.startsWith('image/')
      const raw: UploadItem = {
        id: nextId++,
        name: file.name,
        progress: 0,
        status: 'queued',
        preview:
          isImage && typeof URL.createObjectURL === 'function'
            ? URL.createObjectURL(file)
            : undefined,
        isVideo: file.type.startsWith('video/'),
        captureState: 'pending',
        captureInstant: null,
        captureCivil: null,
        captureSource: null,
        captureUnknown: false,
      }
      files.set(raw.id, file)
      items.push(raw)
      // CRITICAL: queue + return the REACTIVE element (items[i]), not the raw object. pump()/
      // onProgress mutate this — mutating the raw bypasses Vue's proxy so progress/status updates
      // never re-render (the upload finishes but the UI sits at 0% until another change repaints).
      const item = items[items.length - 1]
      pending.push(item)
      created.push(item)
    }
    pump()
    return created
  }

  // THE single release path for a queue member — every removal (one tile or the whole queue) goes
  // through here, so an object URL can never leak. A still-uploading file keeps uploading
  // server-side (pump() already handed uploadFile its own File reference) but, since it's no longer
  // in the queue, its upload_uid won't be posted → it expires (finalize-on-post). A not-yet-started
  // file is also dropped from `pending`: it no longer has an entry in `files`, so leaving it queued
  // would make pump() dereference a File that was just released.
  function release(it: UploadItem): void {
    if (it.preview && typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(it.preview)
    // Stop this tile's inspection poll: the fence would drop its answer anyway, but a removed
    // tile has no business still asking.
    captureAborts.get(it.id)?.abort()
    captureAborts.delete(it.id)
    files.delete(it.id)
    const p = pending.findIndex((q) => q.id === it.id)
    if (p >= 0) pending.splice(p, 1)
  }

  // Drop a tile from the queue.
  function remove(id: number): void {
    const i = items.findIndex((it) => it.id === id)
    if (i < 0) return
    const [it] = items.splice(i, 1)
    release(it)
  }

  // Drop every tile (after a post/edit saves, or when an edit is re-opened). Views MUST call this
  // instead of `items.length = 0`, which would strand every preview URL and File reference.
  function clear(): void {
    for (const it of items.splice(0, items.length)) release(it)
  }

  // The owning scope's teardown is a release site too: a component unmounted with items still
  // staged (a MemoCard destroyed by a feed refresh mid-edit, a route change away from the editor)
  // would otherwise strand its preview URLs and File references for the page's lifetime
  // (MEMNG-87). Guarded, because a queue built outside any scope has nothing to own its
  // teardown — there the explicit remove()/clear() contract remains the whole story.
  if (getCurrentScope()) {
    liveQueues.add(items)
    onScopeDispose(() => {
      clear()
      liveQueues.delete(items)
    })
  }

  return { items, add, remove, clear }
}
