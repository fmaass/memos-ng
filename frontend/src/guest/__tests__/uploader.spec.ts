// @vitest-environment node
import { IDBFactory } from 'fake-indexeddb'
import { describe, expect, it } from 'vitest'
import { createGuestApi, type Receipt } from '../api'
import { openBindings, type BindingStore } from '../bindings'
import { CredentialStore } from '../credential'
import { digestFile } from '../digest'
import { GuestUploader } from '../uploader'
import { bytes, FakeDoor, fileOf, sha } from './fakeDoor'

function memoryStorage(): Storage {
  const data = new Map<string, string>()
  return {
    get length() {
      return data.size
    },
    clear: () => data.clear(),
    getItem: (k) => data.get(k) ?? null,
    key: (i) => [...data.keys()][i] ?? null,
    removeItem: (k) => void data.delete(k),
    setItem: (k, v) => void data.set(k, String(v)),
  }
}

interface Setup {
  door: FakeDoor
  uploader: GuestUploader
  credentials: CredentialStore
  bindings: BindingStore
  receipts: Receipt[]
  refused: number[]
  hashed: string[]
}

async function setup(door = new FakeDoor(), factory = new IDBFactory(), sleep?: (ms: number) => Promise<void>): Promise<Setup> {
  const credentials = new CredentialStore(memoryStorage())
  credentials.set('trip_dev_one', 'dev1')
  const bindings = await openBindings(factory)
  const receipts: Receipt[] = []
  const refused: number[] = []
  const hashed: string[] = []
  const uploader = new GuestUploader({
    api: createGuestApi(door.fetch),
    credentials,
    bindings,
    hash: (file, onProgress) => {
      hashed.push(file.name)
      return digestFile(file, onProgress, 1500)
    },
    limits: door.limits,
    deviceId: 'dev1',
    sleep: sleep ?? (async () => {}),
    onReceipt: (r) => receipts.push(r),
    onRefused: (s) => refused.push(s),
  })
  return { door, uploader, credentials, bindings, receipts, refused, hashed }
}

async function idle(uploader: GuestUploader): Promise<void> {
  for (let i = 0; i < 2000; i++) {
    await new Promise((r) => setTimeout(r, 0))
    if (!uploader.inFlight) return
  }
  throw new Error('uploader never went idle')
}

const tick = () => new Promise((r) => setTimeout(r, 0))

describe('guest uploader', () => {
  it('hashes before create, sends in slices, completes once and forgets its bindings', async () => {
    const s = await setup()
    const a = bytes(10_000, 1)
    const b = bytes(3_000, 2)
    s.uploader.add([fileOf(a, 'a.jpg'), fileOf(b, 'b.jpg')])
    await idle(s.uploader)
    const creates = s.door.calls('POST', '/api/guest/uploads')
    expect(creates).toHaveLength(2)
    expect(s.door.calls('PATCH', '/api/guest/uploads/').every((w) => (w.bytes ?? 0) <= 4096)).toBe(true)
    expect(s.door.calls('POST', '/api/guest/memos')).toHaveLength(1)
    expect(s.receipts).toHaveLength(1)
    expect(s.door.received).toEqual(new Set([sha(a), sha(b)]))
    expect(s.uploader.items.map((i) => i.state)).toEqual(['received', 'received'])
    expect(await s.bindings.list('dev1')).toEqual([])
    expect(await s.bindings.getPending('dev1')).toBeNull()
    expect(s.door.wire.every((w) => w.referrer === 'no-referrer')).toBe(true)
  })

  it('refuses a file over the limit before hashing it', async () => {
    const door = new FakeDoor()
    door.limits = { ...door.limits, max_file_bytes: 1000 }
    const s = await setup(door)
    s.uploader.add([fileOf(bytes(1001, 1), 'big.jpg')])
    await idle(s.uploader)
    expect(s.uploader.items[0].state).toBe('too-large')
    expect(s.hashed).toEqual([])
    expect(door.wire).toEqual([])
  })

  it('never PATCHes a file the pass already holds and leaves it out of the completion', async () => {
    const door = new FakeDoor()
    const a = bytes(5000, 3)
    door.received.add(sha(a))
    const s = await setup(door)
    s.uploader.add([fileOf(a, 'a.jpg')])
    await idle(s.uploader)
    expect(s.uploader.items[0].state).toBe('received')
    expect(door.calls('PATCH', '/')).toEqual([])
    expect(door.calls('POST', '/api/guest/memos')).toEqual([])
  })

  it('resumes after a tab kill at exactly the HEAD offset, mid-slice, without restarting', async () => {
    const factory = new IDBFactory()
    const door = new FakeDoor()
    const data = bytes(12_000, 4)
    door.cutNextPatchAt = 1000 // the second PATCH dies after 1000 of its bytes landed
    let patches = 0
    const origFetch = door.fetch
    door.fetch = async (input, init) => {
      if (init.method === 'PATCH' && ++patches === 1) return origFetch(input, init)
      if (init.method === 'PATCH' && patches === 2) door.cutNextPatchAt = 1000
      return origFetch(input, init)
    }
    door.cutNextPatchAt = null
    const killed = await setup(door, factory, () => new Promise(() => {})) // the tab dies: no retry
    killed.uploader.add([fileOf(data, 'trip.jpg')])
    for (let i = 0; i < 200 && door.wire.filter((w) => w.status === -1).length === 0; i++) await tick()
    killed.uploader.reset()
    const uid = [...door.uploads.keys()][0]
    expect(door.uploads.get(uid)!.data.length).toBe(4096 + 1000)

    const reopened = await setup(door, factory)
    const before = door.wire.length
    reopened.uploader.add([fileOf(data, 'trip.jpg')])
    await idle(reopened.uploader)
    const after = door.wire.slice(before)
    const head = after.find((w) => w.method === 'HEAD')!
    const firstPatch = after.find((w) => w.method === 'PATCH')!
    expect(head.path).toBe(`/api/guest/uploads/${uid}`)
    expect(firstPatch.path).toBe(`/api/guest/uploads/${uid}`)
    expect(firstPatch.offset).toBe(5096) // the committed prefix is kept
    expect(after.filter((w) => w.method === 'POST' && w.path === '/api/guest/uploads')).toEqual([])
    expect(door.received.has(sha(data))).toBe(true)
  })

  it('a decoy that matches every candidate field but the tail gets a fresh upload, never the old one', async () => {
    const factory = new IDBFactory()
    const door = new FakeDoor()
    const real = bytes(1024 * 1024 + 5000, 5)
    const decoy = real.slice()
    decoy[decoy.length - 1] ^= 0xff
    door.gate = new Promise(() => {}) // the first PATCH never answers: the tab is killed
    const killed = await setup(door, factory)
    killed.uploader.add([fileOf(real, 'IMG_1.jpg')])
    for (let i = 0; i < 500 && door.calls('PATCH', '/').length === 0; i++) await tick()
    killed.uploader.reset()
    const oldUid = [...door.uploads.keys()][0]
    door.gate = null
    const reopened = await setup(door, factory)
    reopened.uploader.add([fileOf(decoy, 'IMG_1.jpg')])
    await idle(reopened.uploader)
    expect(reopened.uploader.items[0].recognised).toBe(true) // the candidate matched …
    const patchesToOld = door.wire.filter((w) => w.method === 'PATCH' && w.path.endsWith(oldUid))
    expect(patchesToOld).toHaveLength(1) // … only the killed tab's own PATCH ever went to it
    expect(door.calls('POST', '/api/guest/uploads')).toHaveLength(2)
    expect(door.received.has(sha(decoy))).toBe(true)
  })

  it('the same bytes selected twice are ONE create and ONE stream (joined)', async () => {
    const s = await setup()
    const a = bytes(9000, 6)
    s.uploader.add([fileOf(a, 'a.jpg'), fileOf(a, 'a copy.jpg')])
    await idle(s.uploader)
    expect(s.door.calls('POST', '/api/guest/uploads')).toHaveLength(1)
    const sent = s.door.calls('PATCH', '/').reduce((n, w) => n + (w.bytes ?? 0), 0)
    expect(sent).toBe(9000)
    expect(s.door.calls('POST', '/api/guest/memos')).toHaveLength(1)
    expect(s.uploader.items.map((i) => i.state)).toEqual(['received', 'received'])
  })

  it('two overlapping completions are one request on the wire (joined)', async () => {
    const door = new FakeDoor()
    let release!: () => void
    door.completeHang = new Promise((r) => (release = r))
    const s = await setup(door)
    s.uploader.add([fileOf(bytes(100, 7), 'a.jpg')])
    for (let i = 0; i < 500 && door.calls('POST', '/api/guest/memos').length === 0; i++) await tick()
    const second = s.uploader.complete()
    release()
    door.completeHang = null
    await second
    await idle(s.uploader)
    expect(door.calls('POST', '/api/guest/memos')).toHaveLength(1)
    expect(s.receipts).toHaveLength(1)
  })

  it('a receipt is accepted once, whoever delivers it', async () => {
    const s = await setup()
    s.uploader.add([fileOf(bytes(100, 8), 'a.jpg')])
    await idle(s.uploader)
    const receipt = s.receipts[0]
    const accept = (s.uploader as unknown as { accept: (...a: unknown[]) => Promise<void> }).accept.bind(s.uploader)
    const fence = { credential: s.credentials.get()!, generation: 0 }
    await accept(receipt, { deviceId: 'dev1', uploads: [], caption: null }, fence)
    expect(s.receipts).toHaveLength(1)
  })

  it('a stale refusal never clears the newer credential', async () => {
    const door = new FakeDoor()
    let release!: () => void
    door.gate = new Promise((r) => (release = r))
    const s = await setup(door)
    s.uploader.add([fileOf(bytes(100, 9), 'a.jpg')])
    for (let i = 0; i < 500 && door.calls('PATCH', '/').length === 0; i++) await tick()
    door.tokens = new Set(['trip_dev_two'])
    s.credentials.set('trip_dev_two', 'dev1') // re-joined while the old request was in flight
    door.patchStatus = 401
    release()
    await tick()
    await tick()
    expect(s.credentials.get()?.token).toBe('trip_dev_two')
    expect(s.refused).toEqual([])
  })

  it('an uploader keeps to the credential it was built for: a switch leaves its queue unsent', async () => {
    const door = new FakeDoor()
    let release!: () => void
    door.gate = new Promise((r) => (release = r))
    door.tokens = new Set(['trip_dev_one', 'trip_dev_two'])
    const s = await setup(door)
    s.uploader.add([fileOf(bytes(100, 41), 'a.jpg'), fileOf(bytes(100, 42), 'b.jpg'), fileOf(bytes(100, 43), 'c.jpg')])
    for (let i = 0; i < 500 && door.calls('PATCH', '/').length < 2; i++) await tick()
    expect(door.calls('PATCH', '/')).toHaveLength(2) // two held on the wire, the third file queued
    s.credentials.set('trip_dev_two', 'dev1') // another pass joined on this page
    door.gate = null
    release()
    for (let i = 0; i < 50; i++) await tick()
    expect(door.wire.filter((w) => w.token === 'trip_dev_two')).toEqual([])
    expect(door.calls('POST', '/api/guest/uploads')).toHaveLength(2)
  })

  it('a file that finishes while a completion is in flight is completed after it', async () => {
    const door = new FakeDoor()
    let release!: () => void
    door.completeHang = new Promise((r) => (release = r))
    const s = await setup(door)
    s.uploader.add([fileOf(bytes(100, 51), 'a.jpg')])
    for (let i = 0; i < 500 && door.calls('POST', '/api/guest/memos').length === 0; i++) await tick()
    s.uploader.add([fileOf(bytes(100, 52), 'b.jpg')])
    for (let i = 0; i < 500 && s.uploader.items[1].state !== 'uploaded'; i++) await tick()
    expect(s.uploader.items[1].state).toBe('uploaded')
    door.completeHang = null
    release()
    await idle(s.uploader)
    expect(s.uploader.items.map((i) => i.state)).toEqual(['received', 'received'])
    expect(door.calls('POST', '/api/guest/memos')).toHaveLength(2)
  })

  it('rounds that discard uploads do not use up the retry budget: the valid file is completed', async () => {
    const door = new FakeDoor()
    door.unsupported = (u) => u.filename.startsWith('bad')
    const s = await setup(door)
    const bad = [1, 2, 3, 4, 5, 6].map((n) => fileOf(bytes(100, 60 + n), `bad${n}.jpg`))
    s.uploader.add([...bad, fileOf(bytes(100, 70), 'good.jpg')])
    await idle(s.uploader)
    expect(door.calls('POST', '/api/guest/memos')).toHaveLength(7) // one set, shrinking by one a round
    expect(s.uploader.items.map((i) => i.state)).toEqual([...Array(6).fill('unsupported'), 'received'])
  })

  it('a manual retry after uncertain PATCHes asks HEAD before sending a byte', async () => {
    const door = new FakeDoor()
    door.limits = { max_file_bytes: 1 << 30, chunk_size: 1000, max_patch_bytes: 1000 }
    door.cutEveryPatch = true // each PATCH lands on the server, its answer never arrives
    const s = await setup(door)
    s.uploader.add([fileOf(bytes(9000, 81), 'a.jpg')])
    await idle(s.uploader)
    const item = s.uploader.items[0]
    expect(item.state).toBe('failed')
    const uid = item.uid!
    expect(door.uploads.get(uid)!.data.length).toBe(9000) // the server is ahead of the page
    expect(item.sent).toBe(8000)
    door.cutEveryPatch = false
    const from = door.wire.length
    s.uploader.retry(item)
    await idle(s.uploader)
    const after = door.wire.slice(from).filter((w) => w.path.endsWith(uid))
    expect(after[0]?.method).toBe('HEAD')
    expect(after.filter((w) => w.method === 'PATCH')).toEqual([])
    expect(item.state).toBe('received')
  })

  it('identical bytes picked twice are judged together after a 415: one upload, never re-sent', async () => {
    const door = new FakeDoor()
    door.unsupported = () => true
    const s = await setup(door)
    const data = bytes(9000, 82)
    s.uploader.add([fileOf(data, 'x.jpg'), fileOf(data, 'x.jpg')])
    await idle(s.uploader)
    expect(s.uploader.items.map((i) => i.state)).toEqual(['unsupported', 'unsupported'])
    expect(door.calls('POST', '/api/guest/uploads')).toHaveLength(1)
    expect(door.calls('PATCH', '/').reduce((n, w) => n + (w.bytes ?? 0), 0)).toBe(9000)
    expect(door.calls('POST', '/api/guest/memos')).toHaveLength(1)
  })

  it('a current refusal ends the credential that earned it', async () => {
    const door = new FakeDoor()
    door.patchStatus = 410
    const s = await setup(door)
    s.uploader.add([fileOf(bytes(100, 10), 'a.jpg')])
    for (let i = 0; i < 500 && s.refused.length === 0; i++) await tick()
    expect(s.refused).toEqual([410])
    expect(s.credentials.get()).toBeNull()
  })

  it('a response that outlived a reset resurrects nothing', async () => {
    const door = new FakeDoor()
    let release!: () => void
    door.gate = new Promise((r) => (release = r))
    const s = await setup(door)
    s.uploader.add([fileOf(bytes(100, 11), 'a.jpg')])
    for (let i = 0; i < 500 && door.calls('PATCH', '/').length === 0; i++) await tick()
    const item = s.uploader.items[0]
    s.uploader.reset()
    release()
    for (let i = 0; i < 20; i++) await tick()
    expect(item.state).toBe('sending')
    expect(item.sent).toBe(0)
    expect((await s.bindings.list('dev1'))[0].offset).toBe(0)
    expect(door.calls('POST', '/api/guest/memos')).toEqual([])
  })

  it('duplicate filenames with one damaged: only that UID is invalidated, and it is re-sent once', async () => {
    const door = new FakeDoor()
    const good = bytes(3000, 12)
    const bad = bytes(3000, 13)
    let damaged = false
    door.damage = (u) => !damaged && u.sha256 === sha(bad) && (damaged = true)
    const s = await setup(door)
    s.uploader.add([fileOf(good, 'IMG_0001.jpg'), fileOf(bad, 'IMG_0001.jpg')])
    await idle(s.uploader)
    const completions = door.calls('POST', '/api/guest/memos')
    expect(completions).toHaveLength(3) // refused, survivors, the one re-sent
    expect(completions[0].uids).toHaveLength(2)
    expect(completions[1].uids).toHaveLength(1)
    expect(s.door.received).toEqual(new Set([sha(good), sha(bad)]))
    expect(s.hashed.filter((n) => n === 'IMG_0001.jpg')).toHaveLength(3) // the damaged one re-hashed
    expect(door.calls('POST', '/api/guest/uploads')).toHaveLength(3)
    expect(s.uploader.items.map((i) => i.state)).toEqual(['received', 'received'])
  })

  it('a discard committed whose response was lost: the replay 409s, HEAD reconciles, survivors complete', async () => {
    const door = new FakeDoor()
    const good = bytes(2000, 14)
    const bad = bytes(2000, 15)
    let damaged = false
    door.damage = (u) => !damaged && u.sha256 === sha(bad) && (damaged = true)
    door.loseNextCompletionResponse = true
    const s = await setup(door)
    s.uploader.add([fileOf(good, 'a.jpg'), fileOf(bad, 'b.jpg')])
    await idle(s.uploader)
    const statuses = door.calls('POST', '/api/guest/memos').map((w) => w.status)
    expect(statuses.slice(0, 3)).toEqual([-1, 409, 200])
    expect(door.received).toEqual(new Set([sha(good), sha(bad)]))
  })

  it('a 415 marks the file unsupported and never re-sends it', async () => {
    const door = new FakeDoor()
    const fake = bytes(2000, 16)
    door.unsupported = (u) => u.sha256 === sha(fake)
    const s = await setup(door)
    s.uploader.add([fileOf(bytes(2000, 17), 'a.jpg'), fileOf(fake, 'b.jpg')])
    await idle(s.uploader)
    expect(s.uploader.items.map((i) => i.state)).toEqual(['received', 'unsupported'])
    const creates = door.calls('POST', '/api/guest/uploads')
    expect(creates).toHaveLength(2)
  })

  it('a request-validation 422 (list detail) fails the set without reconciling', async () => {
    const door = new FakeDoor()
    const orig = door.fetch
    door.fetch = async (input, init) =>
      input === '/api/guest/memos' ? new Response(JSON.stringify({ detail: [{ msg: 'bad' }] }), { status: 422 }) : orig(input, init)
    const s = await setup(door)
    s.uploader.add([fileOf(bytes(100, 18), 'a.jpg')])
    await idle(s.uploader)
    expect(s.uploader.items[0].state).toBe('failed')
    expect(door.calls('HEAD', '/')).toEqual([])
  })

  it('persists the completion set before sending it, and a reload replays it first', async () => {
    const factory = new IDBFactory()
    const door = new FakeDoor()
    door.completeHang = new Promise(() => {}) // the tab dies with the completion in flight
    const first = await setup(door, factory)
    first.uploader.add([fileOf(bytes(500, 19), 'a.jpg')])
    for (let i = 0; i < 500 && door.calls('POST', '/api/guest/memos').length === 0; i++) await tick()
    const pending = await first.bindings.getPending('dev1')
    expect(pending?.uploads.map((u) => u.uid)).toEqual(door.calls('POST', '/api/guest/memos')[0].uids)
    first.uploader.reset()
    door.completeHang = null
    const reopened = await setup(door, factory)
    const before = door.wire.length
    await reopened.uploader.resumePending()
    const after = door.wire.slice(before)
    expect(after[0].method).toBe('POST')
    expect(after[0].path).toBe('/api/guest/memos')
    expect(reopened.receipts).toHaveLength(1)
    expect(await reopened.bindings.getPending('dev1')).toBeNull()
  })
})
