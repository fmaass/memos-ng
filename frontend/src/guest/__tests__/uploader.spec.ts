// @vitest-environment node
import { IDBFactory } from 'fake-indexeddb'
import { describe, expect, it } from 'vitest'
import { createGuestApi, type Receipt } from '../api'
import { openBindings, type BindingStore } from '../bindings'
import { CredentialStore } from '../credential'
import { digestFile } from '../digest'
import { GuestUploader, type Connectivity } from '../uploader'
import { bytes, FakeDoor, fileOf, sha, type Fault } from './fakeDoor'

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

async function setup(
  door = new FakeDoor(),
  factory = new IDBFactory(),
  sleep?: (ms: number) => Promise<void>,
  network?: Connectivity,
): Promise<Setup> {
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
    network,
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

  it('six completion rounds that settle nothing leave the set failed with a Retry that completes it', async () => {
    const door = new FakeDoor()
    const orig = door.fetch
    let unreachable = true
    door.fetch = async (input, init) => {
      if (input === '/api/guest/memos' && unreachable) {
        door.wire.push({ method: init.method ?? 'GET', path: input, token: null, status: 0, referrer: '' })
        throw new TypeError('network connection was lost')
      }
      return orig(input, init)
    }
    const s = await setup(door)
    s.uploader.add([fileOf(bytes(100, 31), 'a.jpg')])
    await idle(s.uploader)
    expect(door.calls('POST', '/api/guest/memos')).toHaveLength(6)
    const [item] = s.uploader.items
    expect(item.state).toBe('failed') // not left 'uploaded' ("Sent, confirming") with nothing to press
    expect(item.message).toBeTruthy()
    expect(s.receipts).toEqual([])

    unreachable = false
    s.uploader.retry(item)
    await idle(s.uploader)
    expect(item.state).toBe('received')
    expect(s.receipts).toHaveLength(1)
    expect(door.received).toEqual(new Set([sha(bytes(100, 31))]))
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

// Time here is discrete: it moves only when the earliest pending timer fires, so a 60 s outage takes
// no real time and a request "during" it is one that really met it.
function virtualClock() {
  const timers: { at: number; run: () => void }[] = []
  const clock = {
    now: 0,
    slept: [] as number[],
    sleep: (ms: number) =>
      new Promise<void>((resolve) => {
        clock.slept.push(ms)
        timers.push({ at: clock.now + ms, run: resolve })
      }),
    at: (when: number, run: () => void) => void timers.push({ at: when, run }),
    fire: () => {
      timers.sort((a, b) => a.at - b.at)
      const timer = timers.shift()
      if (!timer) return false
      clock.now = Math.max(clock.now, timer.at)
      timer.run()
      return true
    },
  }
  return clock
}

function fakeNetwork(online = true) {
  const listeners = new Set<() => void>()
  let state = online
  return {
    listeners,
    online: () => state,
    onOnline: (listener: () => void) => {
      listeners.add(listener)
      return () => void listeners.delete(listener)
    },
    set: (next: boolean) => {
      state = next
      if (next) for (const listener of [...listeners]) listener()
    },
  }
}

const settle = async () => {
  for (let i = 0; i < 12; i++) await tick()
}

/** Lets the uploader run until it goes idle, moving time only when it has nothing else to do. */
async function drive(uploader: GuestUploader, clock: ReturnType<typeof virtualClock>): Promise<void> {
  for (let i = 0; i < 2000; i++) {
    await settle()
    if (!uploader.inFlight) return
    clock.fire()
  }
  throw new Error('uploader never went idle')
}

async function rounds(n: number, clock: ReturnType<typeof virtualClock>): Promise<void> {
  await settle()
  for (let i = 0; i < n; i++) {
    clock.fire()
    await settle()
  }
}

const creates = (door: FakeDoor) => door.calls('POST', '/api/guest/uploads')
const opened = (door: FakeDoor) => creates(door).filter((w) => w.status === 201)
const BACKOFFS = [1000, 2000, 4000, 8000, 16_000, 30_000, 30_000, 30_000]

/** An open upload the device already knows about, so its next selection asks HEAD first. */
async function seedBinding(s: Setup, data: Uint8Array<ArrayBuffer>, kept: number): Promise<File> {
  const file = fileOf(data, 'a.jpg')
  const digest = await digestFile(file, () => {}, 1500)
  s.door.uploads.set('u900', { uid: 'u900', filename: 'a.jpg', size: data.length, sha256: digest.sha256, data: data.slice(0, kept) })
  await s.bindings.put({
    deviceId: 'dev1',
    sha256: digest.sha256,
    uploadUid: 'u900',
    size: data.length,
    offset: kept,
    state: 'open',
    name: 'a.jpg',
    lastModified: file.lastModified,
    head: digest.head,
  })
  return file
}

describe('guest uploader through a drop', () => {
  it.each<[string, Fault, boolean]>([
    ['a dropped connection', 'network', true],
    ['a door behind a 502', { status: 502 }, true],
    ['a browser that knows it is offline', 'network', false],
  ])('a 60 s outage of %s spends none of a 50-item queue', async (_label, fault, online) => {
    const door = new FakeDoor()
    const clock = virtualClock()
    const net = fakeNetwork()
    const data = Array.from({ length: 50 }, (_, i) => bytes(2000, 100 + i))
    const orig = door.fetch
    let began: number | null = null
    door.fetch = async (input, init) => {
      const answered = await orig(input, init)
      // Opens right after the fifth create lands, whatever else is on the wire.
      if (began === null && opened(door).length === 5) {
        began = clock.now
        door.outage = fault
        net.set(online)
        clock.at(began + 60_000, () => {
          door.outage = null
          net.set(true)
        })
      }
      return answered
    }
    const s = await setup(door, undefined, clock.sleep, net)
    s.uploader.add(data.map((d, i) => fileOf(d, `IMG_${i}.jpg`)))
    await drive(s.uploader, clock)

    expect(began).not.toBeNull()
    expect(clock.now).toBeGreaterThanOrEqual(began! + 60_000)
    const met = fault === 'network' ? -1 : (fault as { status: number }).status
    expect(creates(door).filter((w) => w.status === met).length).toBeGreaterThanOrEqual(2) // it really met the outage
    expect(s.uploader.items.map((i) => i.state)).toEqual(Array(50).fill('received'))
    expect(door.received).toEqual(new Set(data.map(sha)))
    expect(opened(door)).toHaveLength(50)
    expect(new Set(opened(door).map((w) => w.sha256)).size).toBe(50)
    expect(door.uploads.size).toBe(0) // nothing left open at the door
    expect(s.receipts).toHaveLength(1)
    expect(net.listeners.size).toBe(0)
  }, 30_000)

  it.each<[string, Fault[], number[]]>([
    ['a network error', ['network', 'network'], [1000, 2000]],
    ['a 5xx', [{ status: 502 }, { status: 500, detail: 'boom' }], [1000, 2000]],
    ['a 503 with Retry-After (the pool answer)', [{ status: 503, retryAfter: 5, detail: 'Database busy; retry later' }], [5000]],
    ['a 429 with Retry-After', [{ status: 429, retryAfter: 7 }], [7000]],
    ['a 429 without Retry-After', [{ status: 429 }], [1000]],
  ])('create retries %s with the backoff and ends received on one upload', async (_label, faults, waits) => {
    const door = new FakeDoor()
    door.createFaults = [...faults]
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    s.uploader.add([fileOf(bytes(500, 201), 'a.jpg')])
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('received')
    expect(clock.slept).toEqual(waits)
    expect(creates(door)).toHaveLength(faults.length + 1)
    expect(opened(door)).toHaveLength(1)
  })

  it.each<[string, Fault[], number[]]>([
    ['a network error', ['network'], [1000]],
    ['a 5xx', [{ status: 502 }, { status: 503 }], [1000, 2000]],
    ['a 429 with Retry-After', [{ status: 429, retryAfter: 7 }], [7000]],
    ['a 429 without Retry-After', [{ status: 429 }], [1000]],
  ])('HEAD retries %s and resumes from the answered offset without a create', async (_label, faults, waits) => {
    const door = new FakeDoor()
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    const file = await seedBinding(s, bytes(3000, 202), 1000)
    door.headFaults = [...faults]
    s.uploader.add([file])
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('received')
    expect(clock.slept).toEqual(waits)
    expect(door.calls('HEAD', '/')).toHaveLength(faults.length + 1)
    expect(door.calls('PATCH', '/')[0].offset).toBe(1000)
    expect(creates(door)).toEqual([])
  })

  it('a resync HEAD after a broken PATCH honours Retry-After too', async () => {
    const door = new FakeDoor()
    door.cutNextPatchAt = 100
    door.headFaults = [{ status: 429, retryAfter: 7 }]
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    s.uploader.add([fileOf(bytes(3000, 203), 'a.jpg')])
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('received')
    expect(clock.slept).toEqual([1000, 7000]) // the PATCH's own backoff, then the HEAD's
  })

  it.each<[number, string]>([
    [400, 'failed'],
    [403, 'failed'],
    [404, 'failed'],
    [409, 'failed'], // create_upload raises no 409; nothing says one would clear by itself
    [422, 'failed'],
    [413, 'too-large'],
    [415, 'unsupported'],
    [507, 'quota'], // a 5xx by number, a verdict by meaning
  ])('a %i on create is a refusal: one request, no wait, state %s', async (status, state) => {
    const door = new FakeDoor()
    door.createFaults = [{ status, detail: `refused ${status}` }]
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    s.uploader.add([fileOf(bytes(500, 204), 'a.jpg')])
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe(state)
    expect(s.uploader.items[0].message).toBe(`refused ${status}`)
    expect(creates(door)).toHaveLength(1)
    expect(clock.slept).toEqual([])
  })

  it('a file-cap 429 (Retry-After 3600) fails now with the door sentence, not an hour of Checking', async () => {
    const door = new FakeDoor()
    door.createFaults = [{ status: 429, retryAfter: 3600, detail: 'This device has sent all it can' }]
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    s.uploader.add([fileOf(bytes(500, 205), 'a.jpg')])
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('failed')
    expect(s.uploader.items[0].message).toBe('This device has sent all it can')
    expect(creates(door)).toHaveLength(1)
    expect(clock.slept).toEqual([])
  })

  it.each<[number, boolean]>([
    [121, true], // exactly what the whole budget adds up to
    [122, false],
  ])('a Retry-After of %i s is waited out: %s', async (retryAfter, waited) => {
    const door = new FakeDoor()
    door.createFaults = [{ status: 429, retryAfter, detail: 'later' }]
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    s.uploader.add([fileOf(bytes(500, 206), 'a.jpg')])
    await drive(s.uploader, clock)
    expect(clock.slept).toEqual(waited ? [retryAfter * 1000] : [])
    expect(s.uploader.items[0].state).toBe(waited ? 'received' : 'failed')
  })

  it.each<[string, Fault[], number]>([
    ['at once', [{ status: 401, detail: 'no' }], 401],
    ['at once', [{ status: 410, detail: 'no' }], 410],
    ['after a transient failure', ['network', { status: 410, detail: 'no' }], 410],
  ])('a refusal on create reaches refused() %s (%#)', async (_label, faults, status) => {
    const door = new FakeDoor()
    door.createFaults = [...faults]
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    s.uploader.add([fileOf(bytes(500, 207), 'a.jpg')])
    await rounds(faults.length, clock)
    expect(s.refused).toEqual([status])
    expect(s.credentials.get()).toBeNull()
    expect(creates(door)).toHaveLength(faults.length)
    expect(s.uploader.items[0].state).toBe('waiting')
  })

  it('a refusal on HEAD reaches refused() and opens nothing', async () => {
    const door = new FakeDoor()
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    const file = await seedBinding(s, bytes(3000, 208), 1000)
    door.headFaults = [{ status: 410 }]
    s.uploader.add([file])
    await rounds(0, clock)
    expect(s.refused).toEqual([410])
    expect(s.credentials.get()).toBeNull()
    expect(door.calls('HEAD', '/')).toHaveLength(1)
    expect(creates(door)).toEqual([])
    expect(clock.slept).toEqual([])
  })

  it('a stale refusal through a retried create never clears the newer credential', async () => {
    const door = new FakeDoor()
    door.createFaults = ['network']
    const orig = door.fetch
    let release!: () => void
    const held = new Promise<void>((r) => (release = r))
    let attempts = 0
    door.fetch = async (input, init) => {
      if (input === '/api/guest/uploads' && ++attempts === 2) await held // the retry on the wire
      return orig(input, init)
    }
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    s.uploader.add([fileOf(bytes(500, 209), 'a.jpg')])
    await rounds(1, clock)
    expect(attempts).toBe(2)
    door.tokens = new Set(['trip_dev_two']) // the old token now earns a 401
    s.credentials.set('trip_dev_two', 'dev1')
    release()
    await settle()
    expect(s.credentials.get()?.token).toBe('trip_dev_two')
    expect(s.refused).toEqual([])
  })

  it('create spends exactly its budget: eight retries still land, a ninth is not made', async () => {
    const door = new FakeDoor()
    door.createFaults = Array<Fault>(8).fill('network')
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    s.uploader.add([fileOf(bytes(500, 210), 'a.jpg')])
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('received')
    expect(clock.slept).toEqual(BACKOFFS)
    expect(creates(door)).toHaveLength(9)

    door.outage = 'network'
    s.uploader.add([fileOf(bytes(500, 211), 'b.jpg')])
    await drive(s.uploader, clock)
    const item = s.uploader.items[1]
    expect(item.state).toBe('failed')
    expect(item.message).toBe('The connection was lost')
    expect(creates(door)).toHaveLength(18)
    expect(clock.slept).toEqual([...BACKOFFS, ...BACKOFFS])

    door.outage = null // the entry was forgotten: a Retry opens one fresh create
    s.uploader.retry(item)
    await drive(s.uploader, clock)
    expect(item.state).toBe('received')
    expect(opened(door)).toHaveLength(2)
  })

  it('HEAD spends its budget, then ends Failed with the binding kept for a Retry', async () => {
    const door = new FakeDoor()
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    const file = await seedBinding(s, bytes(3000, 212), 1000)
    door.outage = { status: 502 }
    s.uploader.add([file])
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('failed')
    expect(door.calls('HEAD', '/')).toHaveLength(9)
    expect(clock.slept).toEqual(BACKOFFS)
    expect(creates(door)).toEqual([])
    expect((await s.bindings.list('dev1')).map((b) => b.uploadUid)).toEqual(['u900'])
  })

  it('time offline spends no budget: bounded waits, then the budget is still whole', async () => {
    const door = new FakeDoor()
    door.outage = 'network'
    const clock = virtualClock()
    const net = fakeNetwork(false)
    const s = await setup(door, undefined, clock.sleep, net)
    s.uploader.add([fileOf(bytes(500, 213), 'a.jpg')])
    await rounds(12, clock) // six minutes
    expect(s.uploader.items[0].state).toBe('checking')
    expect(creates(door)).toHaveLength(13) // well past the 9 an online outage would have allowed
    expect(clock.slept).toEqual(Array(13).fill(30_000))
    expect(net.listeners.size).toBe(1) // one wait, one listener, however long it lasts

    net.set(true) // the browser says online, the network is still away: backoff is the mechanism
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('failed')
    expect(clock.slept.slice(13)).toEqual(BACKOFFS)
    expect(net.listeners.size).toBe(0)
  })

  it('the online event ends the wait at once', async () => {
    const door = new FakeDoor()
    door.outage = 'network'
    const clock = virtualClock()
    const net = fakeNetwork(false)
    const s = await setup(door, undefined, clock.sleep, net)
    s.uploader.add([fileOf(bytes(500, 214), 'a.jpg')])
    await settle()
    expect(creates(door)).toHaveLength(1)
    door.outage = null
    net.set(true)
    await settle() // no timer fired
    expect(clock.now).toBe(0)
    expect(creates(door)).toHaveLength(2)
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('received')
    expect(net.listeners.size).toBe(0)
  })

  it('a browser that wrongly says offline does not hold the request: the event never fires, the bound does', async () => {
    const door = new FakeDoor()
    const clock = virtualClock()
    const net = fakeNetwork(false)
    const s = await setup(door, undefined, clock.sleep, net)
    // Answered first try: being told offline delays nothing.
    s.uploader.add([fileOf(bytes(500, 215), 'a.jpg')])
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('received')
    expect(clock.slept).toEqual([])

    door.createFaults = ['network']
    s.uploader.add([fileOf(bytes(500, 216), 'b.jpg')])
    await drive(s.uploader, clock)
    expect(s.uploader.items[1].state).toBe('received')
    expect(clock.slept).toEqual([30_000])
    expect(net.listeners.size).toBe(0)
  })

  it('an HTTP answer proves the network is there: a 5xx spends budget even when told offline', async () => {
    const door = new FakeDoor()
    door.outage = { status: 502 }
    const clock = virtualClock()
    const net = fakeNetwork(false)
    const s = await setup(door, undefined, clock.sleep, net)
    s.uploader.add([fileOf(bytes(500, 217), 'a.jpg')])
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('failed')
    expect(clock.slept).toEqual(BACKOFFS)
    expect(net.listeners.size).toBe(0)
  })

  it('a PATCH cut by going offline waits without spending budget, then resumes at HEAD', async () => {
    const door = new FakeDoor()
    const orig = door.fetch
    let cut = false
    door.fetch = async (input, init) => {
      if (init.method === 'PATCH' && !cut) {
        cut = true
        door.outage = 'network'
        net.set(false)
      }
      return orig(input, init)
    }
    const clock = virtualClock()
    const net = fakeNetwork()
    const s = await setup(door, undefined, clock.sleep, net)
    s.uploader.add([fileOf(bytes(3000, 218), 'a.jpg')])
    await rounds(12, clock)
    expect(s.uploader.items[0].state).toBe('sending')
    expect(clock.slept.every((ms) => ms === 30_000)).toBe(true)
    door.outage = null
    net.set(true)
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('received')
    expect(net.listeners.size).toBe(0)
  })

  it('a resync HEAD waits out an offline browser without spending budget', async () => {
    const door = new FakeDoor()
    door.cutNextPatchAt = 100
    const orig = door.fetch
    let cut = false
    door.fetch = async (input, init) => {
      if (init.method === 'HEAD' && !cut) {
        cut = true
        door.outage = 'network'
        net.set(false)
      }
      return orig(input, init)
    }
    const clock = virtualClock()
    const net = fakeNetwork()
    const s = await setup(door, undefined, clock.sleep, net)
    s.uploader.add([fileOf(bytes(3000, 219), 'a.jpg')])
    await rounds(13, clock) // the PATCH's own 1 s, then a dozen bounded waits
    expect(s.uploader.items[0].state).toBe('sending')
    door.outage = null
    net.set(true)
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('received')
  })

  it('a completion cut by going offline waits without spending its rounds', async () => {
    const door = new FakeDoor()
    const orig = door.fetch
    let cut = false
    door.fetch = async (input, init) => {
      if (input === '/api/guest/memos' && !cut) {
        cut = true
        door.outage = 'network'
        net.set(false)
      }
      return orig(input, init)
    }
    const clock = virtualClock()
    const net = fakeNetwork()
    const s = await setup(door, undefined, clock.sleep, net)
    s.uploader.add([fileOf(bytes(500, 220), 'a.jpg')])
    await rounds(12, clock)
    expect(s.uploader.items[0].state).toBe('uploaded') // not handed back as failed after six
    expect(door.calls('POST', '/api/guest/memos')).toHaveLength(13)
    door.outage = null
    net.set(true)
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('received')
    expect(s.receipts).toHaveLength(1)
    expect(net.listeners.size).toBe(0)
  })

  it('reset during a create backoff: the loop sends nothing more', async () => {
    const door = new FakeDoor()
    door.createFaults = ['network', 'network']
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    s.uploader.add([fileOf(bytes(500, 221), 'a.jpg')])
    await settle()
    expect(creates(door)).toHaveLength(1)
    s.uploader.reset()
    await rounds(3, clock)
    expect(door.wire).toHaveLength(1)
    expect(clock.slept).toEqual([1000])
  })

  it('reset during a HEAD backoff: the loop sends nothing more', async () => {
    const door = new FakeDoor()
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    const file = await seedBinding(s, bytes(3000, 222), 1000)
    door.headFaults = ['network', 'network']
    s.uploader.add([file])
    await settle()
    expect(door.calls('HEAD', '/')).toHaveLength(1)
    s.uploader.reset()
    await rounds(3, clock)
    expect(door.wire).toHaveLength(1)
  })

  it('a credential change during a backoff: nothing more is sent, under either token', async () => {
    const door = new FakeDoor()
    door.createFaults = ['network', 'network']
    door.tokens = new Set(['trip_dev_one', 'trip_dev_two'])
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    s.uploader.add([fileOf(bytes(500, 223), 'a.jpg'), fileOf(bytes(500, 224), 'b.jpg')])
    await settle()
    expect(creates(door)).toHaveLength(2) // both workers in their backoff
    s.credentials.set('trip_dev_two', 'dev1')
    await rounds(4, clock)
    expect(door.wire).toHaveLength(2)
    expect(s.refused).toEqual([])
    expect(s.credentials.get()?.token).toBe('trip_dev_two')
    expect(s.uploader.items.map((i) => i.state)).not.toContain('failed') // the answer is dropped
  })

  it('reset during an offline wait: nothing more is sent and the listener goes with the bound', async () => {
    const door = new FakeDoor()
    door.outage = 'network'
    const clock = virtualClock()
    const net = fakeNetwork(false)
    const s = await setup(door, undefined, clock.sleep, net)
    s.uploader.add([fileOf(bytes(500, 225), 'a.jpg')])
    await settle()
    expect(net.listeners.size).toBe(1)
    s.uploader.reset()
    await rounds(1, clock)
    expect(net.listeners.size).toBe(0)
    expect(door.wire).toHaveLength(1)
  })

  it('an abandoned create loop cannot forget the create of the generation after it', async () => {
    const door = new FakeDoor()
    door.createFaults = ['network']
    const orig = door.fetch
    let release!: () => void
    const held = new Promise<void>((r) => (release = r))
    let attempts = 0
    door.fetch = async (input, init) => {
      if (input === '/api/guest/uploads' && ++attempts === 2) await held
      return orig(input, init)
    }
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    const a = bytes(500, 226)
    s.uploader.add([fileOf(a, 'a.jpg')])
    await settle()
    s.uploader.reset()
    s.uploader.add([fileOf(a, 'a.jpg')])
    await settle()
    expect(attempts).toBe(2) // the new generation's create, held on the wire
    await rounds(1, clock) // the old loop wakes, sees its fence fall, and rejects
    expect(attempts).toBe(2)
    s.uploader.add([fileOf(a, 'a copy.jpg')]) // joins the create in flight, or opens a second
    await settle()
    release()
    await drive(s.uploader, clock)
    expect(s.uploader.items.map((i) => i.state)).toEqual(['received', 'received'])
    expect(opened(door)).toHaveLength(1)
  })

  it('same bytes through a failing create: one create at a time, one upload, both received', async () => {
    const door = new FakeDoor()
    door.createFaults = ['network', { status: 502 }]
    const orig = door.fetch
    let inFlight = 0
    let peak = 0
    door.fetch = async (input, init) => {
      if (input !== '/api/guest/uploads') return orig(input, init)
      peak = Math.max(peak, ++inFlight)
      try {
        await tick() // long enough for a second caller to start one of its own
        return await orig(input, init)
      } finally {
        inFlight--
      }
    }
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    const a = bytes(5000, 227)
    s.uploader.add([fileOf(a, 'a.jpg'), fileOf(a, 'a copy.jpg')])
    await drive(s.uploader, clock)
    expect(s.uploader.items.map((i) => i.state)).toEqual(['received', 'received'])
    expect(peak).toBe(1)
    expect(creates(door)).toHaveLength(3)
    expect(clock.slept).toEqual([1000, 2000]) // one loop's waits, not one per caller
    expect(opened(door)).toHaveLength(1)
    expect(door.calls('PATCH', '/').reduce((n, w) => n + (w.bytes ?? 0), 0)).toBe(5000)
  })

  it('same bytes through a spent budget: both Failed, no rejected create joined, a Retry opens one', async () => {
    const door = new FakeDoor()
    door.outage = 'network'
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    const a = bytes(500, 228)
    s.uploader.add([fileOf(a, 'a.jpg'), fileOf(a, 'a copy.jpg')])
    await drive(s.uploader, clock)
    expect(s.uploader.items.map((i) => i.state)).toEqual(['failed', 'failed'])
    expect(creates(door)).toHaveLength(9)

    door.outage = null
    for (const item of [...s.uploader.items]) s.uploader.retry(item)
    await drive(s.uploader, clock)
    expect(s.uploader.items.map((i) => i.state)).toEqual(['received', 'received'])
    expect(opened(door)).toHaveLength(1)
  })

  // The door keys nothing on (device, sha256) (app/ingest/guest.py create_upload), and it lists no
  // open uploads a client could look its lost uid up in. So this one case leaves a row behind, which
  // holds its cap share until the door's 24 h expiry; no client can do better. Pinned, not endorsed.
  it('a create whose response was lost: the retry opens a second row, the first is left to expire', async () => {
    const door = new FakeDoor()
    door.loseNextCreateResponse = true
    const clock = virtualClock()
    const s = await setup(door, undefined, clock.sleep)
    s.uploader.add([fileOf(bytes(3000, 229), 'a.jpg')])
    await drive(s.uploader, clock)
    expect(s.uploader.items[0].state).toBe('received')
    expect(creates(door).map((w) => w.status)).toEqual([-1, 201])
    const [orphan] = door.uploads.keys()
    expect(door.uploads.get(orphan)!.data.length).toBe(0) // not one byte was sent to it
    expect(door.calls('PATCH', '/').every((w) => !w.path.endsWith(orphan))).toBe(true)
    expect(s.receipts).toHaveLength(1)
  })
})
