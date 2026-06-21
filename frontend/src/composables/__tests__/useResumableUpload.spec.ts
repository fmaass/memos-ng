import { afterEach, describe, expect, it, vi } from 'vitest'

import { uploadFile, useUploadQueue } from '@/composables/useResumableUpload'

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

afterEach(() => vi.unstubAllGlobals())

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
    const uploadUid = await uploadFile(fileOf(10), (p) => progress.push(p))

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

  it('remove() drops a tile from the queue', () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonRes({ upload_uid: 'x', chunk_size: 100, bytes_received: 0 })))
    const { items, add, remove } = useUploadQueue()
    const [a, b] = add([fileOf(3, 'a.png', 'image/png'), fileOf(3, 'b.png', 'image/png')])
    expect(items).toHaveLength(2)
    remove(a.id)
    expect(items.map((i) => i.id)).toEqual([b.id]) // only the removed one is gone
  })
})
