import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { effectScope, nextTick, watchEffect } from 'vue'

import {
  applyUploadProgressEvent,
  uploadFile,
  useUploadQueue,
} from '@/composables/useResumableUpload'
import { instantOf } from '@/lib/__tests__/artefact'

// The one dated instant these tests need is the artefact's, by id: the subject here is the FENCE,
// not the rule, and a second spelling of a capture instant is a second place to change it.
const DATED = instantOf('single-exact', 'A')

function jsonRes(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  })
}

function fileOf(size: number, name = 'clip.mp4', type = 'video/mp4') {
  return new File([new Uint8Array(size)], name, { type })
}

// Preview object URLs: happy-dom's real createObjectURL returns an opaque uuid, so pin a
// deterministic one per file — the release assertions match URL against URL.
let objectUrl: { create: MockInstance; revoke: MockInstance }
beforeEach(() => {
  objectUrl = {
    create: vi
      .spyOn(URL, 'createObjectURL')
      .mockImplementation((f) => `blob:mock/${(f as File).name}`),
    revoke: vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {}),
  }
})

// The queue's File map is closure-private; record the ids it deletes while `fn` runs. Scoped to the
// call so unrelated Map usage elsewhere in the suite can't pollute the assertion.
function spyMapDeletes(fn: () => void): unknown[] {
  const spy = vi.spyOn(Map.prototype, 'delete')
  try {
    fn()
    return spy.mock.calls.map((c) => c[0])
  } finally {
    spy.mockRestore()
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('uploadFile (finalize-on-post)', () => {
  it('creates, sends each chunk at the right offset, returns the upload_uid, and does NOT finalize', async () => {
    const patchOffsets: string[] = []
    let finalizeCalls = 0
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET'
      if (url === '/api/uploads' && method === 'POST') {
        const body = JSON.parse(init.body as string)
        expect(body).toMatchObject({ filename: 'clip.mp4', mime: 'video/mp4', size: 10 })
        return jsonRes({ upload_uid: 'u1', chunk_size: 4, bytes_received: 0 })
      }
      if (url === '/api/uploads/u1' && method === 'PATCH') {
        const off = Number((init.headers as Record<string, string>)['Upload-Offset'])
        patchOffsets.push(String(off))
        return jsonRes({ upload_uid: 'u1', chunk_size: 4, bytes_received: Math.min(off + 4, 10) })
      }
      if (url.endsWith('/finalize')) finalizeCalls++ // must NOT happen now
      throw new Error(`unexpected ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const progress: number[] = []
    const created: string[][] = []
    const uploadUid = await uploadFile(
      fileOf(10),
      (p) => progress.push(p),
      // The uid is handed over at the create, before any chunk (MEMNG-965).
      (uid) => created.push([uid, ...patchOffsets]),
    )

    expect(created).toEqual([['u1']])
    expect(patchOffsets).toEqual(['0', '4', '8']) // 10 bytes / 4-byte chunks
    expect(finalizeCalls).toBe(0) // finalize-on-post: the memo create finalizes, not the uploader
    expect(uploadUid).toBe('u1') // returns the upload_uid, not an attachment
    expect(progress).toEqual([40, 80, 100])
  })

  it('resumes from the server offset (HEAD) after a failed chunk', async () => {
    let patchAttempts = 0
    let headCalls = 0
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET'
      if (url === '/api/uploads' && method === 'POST') {
        return jsonRes({ upload_uid: 'u1', chunk_size: 4, bytes_received: 0 })
      }
      if (url === '/api/uploads/u1' && method === 'HEAD') {
        headCalls++
        return new Response(null, { status: 200, headers: { 'Upload-Offset': '0' } })
      }
      if (url === '/api/uploads/u1' && method === 'PATCH') {
        const off = Number((init.headers as Record<string, string>)['Upload-Offset'])
        patchAttempts++
        if (off === 0 && patchAttempts === 1) throw new Error('network blip') // first attempt fails
        return jsonRes({ bytes_received: Math.min(off + 4, 8) })
      }
      throw new Error(`unexpected ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const uploadUid = await uploadFile(fileOf(8))

    expect(headCalls).toBeGreaterThanOrEqual(1) // HEAD-resume was exercised
    expect(uploadUid).toBe('u1')
  })
})

describe('useUploadQueue', () => {
  it('tracks each file to done with its upload_uid (no eager finalize)', async () => {
    const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET'
      if (url === '/api/uploads' && method === 'POST') {
        return jsonRes({ upload_uid: 'uq', chunk_size: 100, bytes_received: 0 })
      }
      if (url === '/api/uploads/uq' && method === 'PATCH') {
        return jsonRes({ bytes_received: 5 })
      }
      throw new Error(`unexpected ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { items, add } = useUploadQueue()
    add([fileOf(5, 'a.png', 'image/png')])
    await vi.waitFor(() => expect(items[0].status).toBe('done'))
    expect(items[0].uploadUid).toBe('uq')
    expect(items[0].progress).toBe(100)
  })

  it('progress mutations are REACTIVE (re-trigger effects, not just update the raw value)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit = {}) => {
        const method = init.method ?? 'GET'
        if (url === '/api/uploads' && method === 'POST')
          return jsonRes({ upload_uid: 'rx', chunk_size: 100, bytes_received: 0 })
        if (url === '/api/uploads/rx' && method === 'PATCH') return jsonRes({ bytes_received: 5 })
        throw new Error(`unexpected ${method} ${url}`)
      }),
    )
    const { items, add } = useUploadQueue()
    add([fileOf(5, 'r.png', 'image/png')])
    const seen: number[] = []
    const stop = watchEffect(() => seen.push(items[0]?.progress ?? -1)) // tracks the proxy
    await vi.waitFor(() => expect(items[0].status).toBe('done'))
    await nextTick()
    stop()
    // The effect must have re-run when progress hit 100 — if pump() mutated the raw object instead
    // of the reactive proxy, the effect would never re-fire and the UI would sit at 0%.
    expect(seen).toContain(100)
  })

  it('remove() drops a tile from the queue', () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonRes({ upload_uid: 'x', chunk_size: 100, bytes_received: 0 })))
    const { items, add, remove } = useUploadQueue()
    const [a, b] = add([fileOf(3, 'a.png', 'image/png'), fileOf(3, 'b.png', 'image/png')])
    expect(items).toHaveLength(2)
    remove(a.id)
    expect(items.map((i) => i.id)).toEqual([b.id]) // only the removed one is gone
  })

  it('remove() revokes the tile preview and drops its File reference', () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonRes({ upload_uid: 'x', chunk_size: 100, bytes_received: 0 })))
    const { add, remove } = useUploadQueue()
    const [a] = add([fileOf(3, 'a.png', 'image/png')])
    const deleted = spyMapDeletes(() => remove(a.id))
    expect(objectUrl.revoke).toHaveBeenCalledWith('blob:mock/a.png')
    expect(deleted).toContain(a.id) // the File left the queue's map
  })

  // MEMNG-46: the views used to clear with `items.length = 0`, which never reached remove() — every
  // queued preview URL and File reference leaked per cleared queue.
  it('clear() revokes EVERY preview object URL and drops EVERY File reference', () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonRes({ upload_uid: 'x', chunk_size: 100, bytes_received: 0 })))
    const { items, add, clear } = useUploadQueue()
    const staged = add([
      fileOf(3, 'a.png', 'image/png'),
      fileOf(3, 'b.png', 'image/png'),
      fileOf(3, 'c.png', 'image/png'),
    ])
    expect(items).toHaveLength(3)
    expect(objectUrl.create).toHaveBeenCalledTimes(3) // three live object URLs exist

    const deleted = spyMapDeletes(clear)

    expect(items).toHaveLength(0) // queue emptied
    expect(objectUrl.revoke.mock.calls.map((c) => c[0]).sort()).toEqual([
      'blob:mock/a.png',
      'blob:mock/b.png',
      'blob:mock/c.png',
    ])
    for (const it of staged) expect(deleted).toContain(it.id) // every File reference dropped
  })

  it('clear() releases a not-yet-started file so it is never uploaded afterwards', async () => {
    const started: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit = {}) => {
        const method = init.method ?? 'GET'
        if (url === '/api/uploads' && method === 'POST') {
          started.push(JSON.parse(init.body as string).filename)
          return jsonRes({ upload_uid: 'u', chunk_size: 100, bytes_received: 0 })
        }
        if (url === '/api/uploads/u' && method === 'PATCH') return jsonRes({ bytes_received: 3 })
        throw new Error(`unexpected ${method} ${url}`)
      }),
    )
    const { items, add, clear } = useUploadQueue(1) // concurrency 1 → the 2nd file waits in `pending`
    const [first, later] = add([fileOf(3, 'first.png', 'image/png'), fileOf(3, 'later.png', 'image/png')])
    expect(started).toEqual(['first.png']) // only the first one got a slot

    clear()
    expect(items).toHaveLength(0)
    await vi.waitFor(() => expect(first.status).toBe('done')) // the in-flight one still finishes…
    await new Promise((r) => setTimeout(r, 0)) // …and its completion re-pumps the queue

    // Release also un-queues the waiting file: its File is gone from the map, so a pump() that
    // still picked it up would dereference a released File. It must never be touched again.
    expect(started).toEqual(['first.png'])
    expect(later.status).toBe('queued') // never moved to 'uploading'/'error' by a stale pump
  })

  // MEMNG-87: release used to run only on explicit remove()/clear() — a component unmounted with
  // items still staged (a MemoCard destroyed by a feed refresh mid-edit, a route change away from
  // the editor) stranded its preview URLs and File references for the page's lifetime.
  it('disposing the owning effect scope releases the queue like clear() would', () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonRes({ upload_uid: 'x', chunk_size: 100, bytes_received: 0 })))
    const scope = effectScope()
    const queue = scope.run(() => useUploadQueue())!
    const staged = queue.add([fileOf(3, 'a.png', 'image/png'), fileOf(3, 'b.png', 'image/png')])
    expect(queue.items).toHaveLength(2)

    const deleted = spyMapDeletes(() => scope.stop())

    expect(queue.items).toHaveLength(0) // torn down through the same per-item release path
    expect(objectUrl.revoke.mock.calls.map((c) => c[0]).sort()).toEqual([
      'blob:mock/a.png',
      'blob:mock/b.png',
    ])
    for (const it of staged) expect(deleted).toContain(it.id) // every File reference dropped
  })

  it('a queue built outside any scope keeps working without a scope-lifecycle warning', () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonRes({ upload_uid: 'x', chunk_size: 100, bytes_received: 0 })))
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { items, add, clear } = useUploadQueue()
    add([fileOf(3, 'a.png', 'image/png')])
    clear()
    expect(items).toHaveLength(0) // explicit release still the contract outside a scope
    // Vue warns "onScopeDispose() is called when there is no active effect scope" if the
    // registration is unguarded — the composable must stay quiet where there is nothing to own it.
    const scopeWarnings = warn.mock.calls.filter((c) => String(c[0]).includes('onScopeDispose'))
    expect(scopeWarnings).toHaveLength(0)
  })

  it('clear() has the SAME in-flight semantics as remove(): the upload keeps running, neither aborts', async () => {
    // The uploader has no AbortController — dropping a tile only detaches it from the queue, so its
    // upload_uid is never posted and the server-side Upload expires (finalize-on-post).
    async function patchCallsAfterDrop(drop: (q: ReturnType<typeof useUploadQueue>, id: number) => void) {
      let release!: () => void
      const gate = new Promise<void>((r) => (release = r))
      const offsets: number[] = []
      vi.stubGlobal(
        'fetch',
        vi.fn(async (url: string, init: RequestInit = {}) => {
          const method = init.method ?? 'GET'
          if (url === '/api/uploads' && method === 'POST')
            return jsonRes({ upload_uid: 'g', chunk_size: 4, bytes_received: 0 })
          if (url === '/api/uploads/g' && method === 'PATCH') {
            const off = Number((init.headers as Record<string, string>)['Upload-Offset'])
            if (off === 0) await gate // hold the first chunk until the queue is dropped
            offsets.push(off)
            return jsonRes({ bytes_received: Math.min(off + 4, 12) })
          }
          throw new Error(`unexpected ${method} ${url}`)
        }),
      )
      const queue = useUploadQueue()
      const [item] = queue.add([fileOf(12, 'big.png', 'image/png')])
      await vi.waitFor(() => expect(item.status).toBe('uploading'))

      drop(queue, item.id) // dropped mid-upload
      expect(queue.items).toHaveLength(0)
      release()
      await vi.waitFor(() => expect(offsets).toEqual([0, 4, 8]))
      vi.unstubAllGlobals()
      return offsets
    }

    const viaRemove = await patchCallsAfterDrop((q, id) => q.remove(id))
    const viaClear = await patchCallsAfterDrop((q) => q.clear())
    expect(viaClear).toEqual(viaRemove) // identical per-item behaviour: no abort in either path
  })
})

describe('the capture inspection is identity-fenced (MEMNG-262)', () => {
  /** create → one append → the capture poll, with the inspection answer under the test's control. */
  function laneWith(capture: () => Promise<Response>) {
    const captureCalls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit = {}) => {
        const method = init.method ?? 'GET'
        if (url === '/api/uploads' && method === 'POST') {
          return jsonRes({ upload_uid: 'u1', chunk_size: 64, bytes_received: 0 })
        }
        if (url === '/api/uploads/u1' && method === 'PATCH') return jsonRes({ bytes_received: 4 })
        if (url === '/api/uploads/u1/capture') {
          captureCalls.push(url)
          return capture()
        }
        throw new Error(`unexpected ${method} ${url}`)
      }),
    )
    return captureCalls
  }

  it('applies the answer to the tile that asked for it', async () => {
    laneWith(async () =>
      jsonRes({
        state: 'done',
        capture_instant: DATED,
        capture_civil: null,
        capture_source: 'exif',
      }),
    )
    const queue = useUploadQueue()
    const [item] = queue.add([fileOf(4, 'pic.jpg', 'image/jpeg')])
    await vi.waitFor(() => expect(item.captureState).toBe('done'))
    expect(item.captureInstant).toBe(DATED)
    expect(item.captureSource).toBe('exif')
  })

  it('drops an answer whose tile is gone — it can never mark another tile done', async () => {
    let answer!: (value: Response) => void
    const pending = new Promise<Response>((resolve) => {
      answer = resolve
    })
    laneWith(() => pending)
    const queue = useUploadQueue()
    const [first] = queue.add([fileOf(4, 'first.jpg', 'image/jpeg')])
    await vi.waitFor(() => expect(first.status).toBe('done'))

    queue.remove(first.id) // removed while its inspection is in flight
    const [second] = queue.add([fileOf(4, 'second.jpg', 'image/jpeg')])
    answer(
      jsonRes({
        state: 'done',
        capture_instant: DATED,
        capture_civil: null,
        capture_source: 'exif',
      }),
    )
    await nextTick()
    await nextTick()

    // the late answer belonged to a tile that no longer exists; the one still on screen keeps its
    // own decision (it has its own poll) and never inherits a stranger's date
    expect(first.captureInstant).toBeNull()
    expect(second.captureInstant).toBeNull()
  })

  it('settles as undated rather than pending for ever when the server keeps saying pending', async () => {
    const calls = laneWith(async () =>
      jsonRes({ state: 'pending', capture_instant: null, capture_civil: null, capture_source: null }),
    )
    // Fake timers from the start — the backoff is deliberately ~20 s of wall clock, and what is
    // being proved is that it ENDS, not how long it takes. (Installed BEFORE the upload, so every
    // wait in the poll is on the fake clock.)
    vi.useFakeTimers()
    try {
      const queue = useUploadQueue()
      const [item] = queue.add([fileOf(4, 'slow.jpg', 'image/jpeg')])
      for (let i = 0; i < 60 && item.captureState !== 'done'; i++) {
        await vi.advanceTimersByTimeAsync(1000)
      }
      expect(item.captureState).toBe('done')
      expect(item.captureInstant).toBeNull() // settled with no date — Save is never blocked
      expect(calls.length).toBeGreaterThan(1) // …and it really did keep asking first
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('upload.progress reaches the tile uploading those bytes (MEMNG-965)', () => {
  it('moves a live queue tile once its upload exists, and stops once the scope is gone', () => {
    const scope = effectScope()
    const queue = scope.run(() => useUploadQueue())!
    queue.items.push({
      id: 99,
      name: 'clip.mov',
      progress: 5,
      status: 'uploading',
      uploadUid: 'u-live',
      captureState: 'pending',
      captureInstant: null,
      captureCivil: null,
      captureSource: null,
      captureUnknown: false,
    })
    const tile = queue.items[0]

    applyUploadProgressEvent({ upload_uid: 'u-live', bytes_received: 40, declared_size: 100 })
    expect(tile.progress).toBe(40)
    applyUploadProgressEvent({ upload_uid: 'u-other', bytes_received: 90, declared_size: 100 })
    expect(tile.progress).toBe(40)

    scope.stop()
    expect(queue.items).toHaveLength(0)
  })
})
