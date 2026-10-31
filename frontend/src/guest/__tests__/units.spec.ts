// @vitest-environment node
import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { createGuestApi, GuestHttpError } from '../api'
import { CREDENTIAL_KEY, CredentialStore } from '../credential'
import { digestFile } from '../digest'
import { Hasher, type WorkerLike } from '../hasher'
import { acceptList, appUrl, extensionAccepted, isIOS } from '../platform'
import { GuestSession, JOINED_PATH, secretFromPath } from '../session'
import { bytes } from './fakeDoor'

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

function refusingStorage(): Storage {
  const storage = memoryStorage()
  storage.setItem = () => {
    throw new DOMException('The quota has been exceeded.', 'QuotaExceededError')
  }
  return storage
}

describe('credential epochs', () => {
  it('reports whether the credential was kept durably', () => {
    expect(new CredentialStore(memoryStorage()).set('trip_dev_a', 'd1').durable).toBe(true)
    expect(new CredentialStore(null).set('trip_dev_a', 'd1').durable).toBe(false)
    const refused = new CredentialStore(refusingStorage()).set('trip_dev_a', 'd1')
    expect(refused.durable).toBe(false)
    expect(refused.token).toBe('trip_dev_a') // still usable for this page
    const dropping = memoryStorage()
    dropping.setItem = () => {} // accepts the write and keeps nothing
    expect(new CredentialStore(dropping).set('trip_dev_a', 'd1').durable).toBe(false)
  })

  it('a refusal earned by an older credential clears nothing', () => {
    const store = new CredentialStore(memoryStorage())
    const old = store.set('trip_dev_a', 'd1')
    store.set('trip_dev_b', 'd1')
    expect(store.clearIfCurrent(old)).toBe(false)
    expect(store.get()?.token).toBe('trip_dev_b')
  })

  it('leaves a credential another tab stored since', () => {
    const storage = memoryStorage()
    const store = new CredentialStore(storage)
    const mine = store.set('trip_dev_a', 'd1')
    storage.setItem(CREDENTIAL_KEY, JSON.stringify({ token: 'trip_dev_other', deviceId: 'd2' }))
    expect(store.clearIfCurrent(mine)).toBe(true)
    expect(storage.getItem(CREDENTIAL_KEY)).toContain('trip_dev_other')
  })

  it('survives a reload through storage', () => {
    const storage = memoryStorage()
    new CredentialStore(storage).set('trip_dev_a', 'd1')
    expect(new CredentialStore(storage).get()?.token).toBe('trip_dev_a')
  })
})

describe('session', () => {
  function harness(path: string, storage = memoryStorage(), ios = false) {
    const wire: { url: string; body: string | null }[] = []
    let current = path
    const replaced: { path: string; stored: string | null }[] = []
    const answers: ((r: Response) => void)[] = []
    const api = createGuestApi(async (url, init) => {
      wire.push({ url, body: (init.body as string) ?? null })
      if (url === '/api/pass/redeem') return new Promise<Response>((r) => answers.push(r))
      return new Response(JSON.stringify({ detail: 'Not a valid guest credential' }), { status: 401 })
    })
    const credentials = new CredentialStore(storage)
    const session = new GuestSession(api, credentials, {
      path: () => current,
      replace: (p) => {
        replaced.push({ path: p, stored: storage.getItem(CREDENTIAL_KEY) })
        current = p
      },
    }, ios)
    const redeemed = () =>
      answers.forEach((answer) => answer(new Response(JSON.stringify({ device_token: 'trip_dev_x', device_id: 'd9', pass: { label: 'T', scope: 's', expires_at: '' }, limits: { max_file_bytes: 1, chunk_size: 1, max_patch_bytes: 1 } }), { status: 201 })))
    return { session, wire, replaced, credentials, redeemed, path: () => current }
  }

  it('reads the secret from the path, never from /p/joined', () => {
    expect(secretFromPath('/p/abc%2Bdef')).toBe('abc+def')
    expect(secretFromPath(JOINED_PATH)).toBeNull()
    expect(secretFromPath('/p/a/b')).toBeNull()
  })

  it('stores the credential BEFORE the secret leaves the address bar, and one redeem joins another', async () => {
    const h = harness('/p/s3cret')
    await h.session.boot()
    expect(h.session.phase).toBe('name')
    const one = h.session.redeem('Anna')
    const two = h.session.redeem('Anna')
    await Promise.resolve()
    h.redeemed()
    await Promise.all([one, two])
    expect(h.wire.filter((w) => w.url === '/api/pass/redeem')).toHaveLength(1)
    expect(h.replaced).toEqual([{ path: JOINED_PATH, stored: expect.stringContaining('trip_dev_x') }])
    expect(h.session.phase).toBe('ready')
  })

  it('keeps the pass link when the credential cannot be kept, and says so', async () => {
    const h = harness('/p/s3cret', refusingStorage())
    await h.session.boot()
    const run = h.session.redeem('Anna')
    await Promise.resolve()
    h.redeemed()
    await run
    expect(h.session.phase).toBe('ready')
    expect(h.replaced).toEqual([])
    expect(h.path()).toBe('/p/s3cret')
    expect(h.session.durable).toBe(false)
  })

  it('/p/joined without a credential asks for the link and redeems nothing', async () => {
    const h = harness(JOINED_PATH)
    await h.session.boot()
    expect(h.session.phase).toBe('missing')
    expect(h.wire).toEqual([])
  })

  it('an iPhone lands on the app offer first', async () => {
    const h = harness('/p/s3cret', memoryStorage(), true)
    await h.session.boot()
    expect(h.session.phase).toBe('landing')
    h.session.chooseBrowser()
    expect(h.session.phase).toBe('name')
  })

  it('a stale 401 on boot does not end a credential stored meanwhile', async () => {
    const storage = memoryStorage()
    const h = harness(JOINED_PATH, storage)
    h.credentials.set('trip_dev_old', 'd1')
    const booting = h.session.boot()
    h.credentials.set('trip_dev_new', 'd1')
    await booting
    expect(h.credentials.get()?.token).toBe('trip_dev_new')
    expect(h.session.phase).not.toBe('ended')
  })
})

describe('session when the browser refuses to keep the credential', () => {
  const limits = { max_file_bytes: 1, chunk_size: 1, max_patch_bytes: 1 }
  // A door that knows two devices: d1 on pass "Old" (already stored) and the one each redeem mints.
  function door() {
    const wire: string[] = []
    let minted = 0
    const devices = new Map<string, { id: string; pass: string }>([['trip_dev_a', { id: 'd1', pass: 'Old' }]])
    const api = createGuestApi(async (url, init) => {
      wire.push(url)
      if (url === '/api/pass/redeem') {
        const secret = (JSON.parse(init.body as string) as { secret: string }).secret
        const token = `trip_dev_new${++minted}`
        devices.set(token, { id: `d-new${minted}`, pass: secret })
        return new Response(JSON.stringify({ device_token: token, device_id: `d-new${minted}`, pass: { label: secret, scope: 'contribute', expires_at: '' }, limits }), { status: 201 })
      }
      const token = new Headers(init.headers).get('Authorization')?.replace('Bearer ', '') ?? ''
      const device = devices.get(token)
      if (url === '/api/guest/me' && device) {
        return new Response(JSON.stringify({ device_id: device.id, display_name: 'Anna', platform: 'web', pass: { label: device.pass, scope: 'contribute', expires_at: '' }, limits, uploads_count: 0, bytes_total: 0 }), { status: 200 })
      }
      return new Response(JSON.stringify({ detail: 'Not a valid guest credential' }), { status: 401 })
    })
    return { api, wire }
  }
  // One page load: the stores are rebuilt from the same browser storage, as a reload does.
  function page(d: ReturnType<typeof door>, path: string, local: Storage, tab: Storage) {
    let current = path
    const credentials = new CredentialStore(local, tab)
    const session = new GuestSession(d.api, credentials, { path: () => current, replace: (p) => void (current = p) }, false)
    return { session, path: () => current }
  }

  it('joining a second pass on a refusing browser restores that pass link, never /p/joined', async () => {
    const local = memoryStorage()
    local.setItem(CREDENTIAL_KEY, JSON.stringify({ token: 'trip_dev_a', deviceId: 'd1' }))
    local.setItem = () => {
      throw new DOMException('The quota has been exceeded.', 'QuotaExceededError')
    }
    const tab = memoryStorage()
    const d = door()
    const first = page(d, '/p/second', local, tab)
    await first.session.boot()
    expect(first.session.deviceId).toBe('d1')
    expect(first.session.otherSecret).toBe('second')
    expect(first.path()).toBe(JOINED_PATH)

    first.session.joinOther()
    await first.session.redeem('Anna')
    expect(first.session.phase).toBe('ready')
    expect(first.session.deviceId).toBe('d-new1')
    expect(first.session.durable).toBe(false)
    expect(first.path()).toBe('/p/second') // the just-joined pass, not /p/joined

    const reloaded = page(d, first.path(), local, tab)
    await reloaded.session.boot()
    expect(reloaded.session.phase).toBe('ready')
    expect(reloaded.session.deviceId).toBe('d-new1') // the pass just joined, not the stored one
    expect(reloaded.session.pass?.label).toBe('second')
    expect(reloaded.path()).not.toBe(JOINED_PATH)
  })

  it('a reload of the kept pass link resumes the same device instead of redeeming a new one', async () => {
    const local = refusingStorage()
    const tab = memoryStorage()
    const d = door()
    const first = page(d, '/p/s3cret', local, tab)
    await first.session.boot()
    await first.session.redeem('Anna')
    expect(first.session.durable).toBe(false)
    expect(first.path()).toBe('/p/s3cret')

    const reloaded = page(d, first.path(), local, tab)
    await reloaded.session.boot()
    expect(reloaded.session.phase).toBe('ready')
    expect(reloaded.session.deviceId).toBe('d-new1')
    expect(reloaded.session.otherSecret).toBeNull() // the same pass: not offered as another one
    expect(reloaded.session.durable).toBe(false) // still says a closed tab cannot resume
    expect(reloaded.path()).toBe('/p/s3cret')
    await reloaded.session.redeem('Anna') // a stray Join after the reload redeems nothing
    expect(d.wire.filter((u) => u === '/api/pass/redeem')).toHaveLength(1)
  })

  it('when no storage keeps the new credential, the older pass is never resumed on /p/joined', async () => {
    const local = memoryStorage()
    local.setItem(CREDENTIAL_KEY, JSON.stringify({ token: 'trip_dev_a', deviceId: 'd1' }))
    local.setItem = () => {
      throw new DOMException('The quota has been exceeded.', 'QuotaExceededError')
    }
    const tab = refusingStorage()
    const d = door()
    const first = page(d, '/p/second', local, tab)
    await first.session.boot()
    first.session.joinOther()
    await first.session.redeem('Anna')
    expect(first.path()).toBe('/p/second')

    const reloaded = page(d, first.path(), local, tab)
    await reloaded.session.boot()
    expect(reloaded.session.deviceId).toBeNull() // not d1 on the old pass
    expect(reloaded.session.phase).toBe('name') // the kept link asks to join again
    expect(reloaded.path()).toBe('/p/second')
  })

  it('a restored pass link carries a secret with reserved characters intact', async () => {
    const local = memoryStorage()
    local.setItem(CREDENTIAL_KEY, JSON.stringify({ token: 'trip_dev_a', deviceId: 'd1' }))
    local.setItem = () => {
      throw new DOMException('The quota has been exceeded.', 'QuotaExceededError')
    }
    const tab = memoryStorage()
    const d = door()
    const first = page(d, '/p/se%2Fc%2Bond%23x', local, tab)
    await first.session.boot()
    first.session.joinOther()
    await first.session.redeem('Anna')
    expect(secretFromPath(first.path())).toBe('se/c+ond#x')
    const reloaded = page(d, first.path(), local, tab)
    await reloaded.session.boot()
    expect(reloaded.session.deviceId).toBe('d-new1')
    expect(reloaded.session.otherSecret).toBeNull()
  })

  it('a write another tab overtook drops only the credential this one replaced, never the other tab\'s', () => {
    const local = memoryStorage()
    local.setItem(CREDENTIAL_KEY, JSON.stringify({ token: 'trip_dev_a', deviceId: 'd1' }))
    const store = new CredentialStore(local, memoryStorage())
    const write = local.setItem.bind(local)
    local.setItem = (k, v) => {
      write(k, v)
      write(k, JSON.stringify({ token: 'trip_dev_other_tab', deviceId: 'd7' })) // the other tab lands last
    }
    expect(store.set('trip_dev_b', 'd2', 's3cret').durable).toBe(false)
    expect(local.getItem(CREDENTIAL_KEY)).toContain('trip_dev_other_tab')
  })

  it('a later durable write supersedes the tab copy', () => {
    const local = memoryStorage()
    const tab = memoryStorage()
    const refusing = new CredentialStore(refusingStorage(), tab)
    refusing.set('trip_dev_tab', 'd1', 's3cret')
    const store = new CredentialStore(local, tab)
    expect(store.get()?.token).toBe('trip_dev_tab')
    expect(store.set('trip_dev_kept', 'd2', 's3cret').durable).toBe(true)
    expect(new CredentialStore(local, tab).get()).toMatchObject({ token: 'trip_dev_kept', durable: true })
  })

  it('a refusal clears the tab copy too, so a reload does not boot a dead credential', () => {
    const tab = memoryStorage()
    const store = new CredentialStore(refusingStorage(), tab)
    const mine = store.set('trip_dev_a', 'd1', 's3cret')
    expect(new CredentialStore(refusingStorage(), tab).get()?.token).toBe('trip_dev_a')
    expect(store.clearIfCurrent(mine)).toBe(true)
    expect(new CredentialStore(refusingStorage(), tab).get()).toBeNull()
  })

  it('a different pass link opened in the same tab is offered, not taken for the kept one', async () => {
    const local = refusingStorage()
    const tab = memoryStorage()
    const d = door()
    const first = page(d, '/p/s3cret', local, tab)
    await first.session.boot()
    await first.session.redeem('Anna')

    const other = page(d, '/p/elsewhere', local, tab)
    await other.session.boot()
    expect(other.session.deviceId).toBe('d-new1')
    expect(other.session.otherSecret).toBe('elsewhere')
  })
})

describe('hasher', () => {
  it('a job answered twice is answered once, and the next job keeps its own answer', async () => {
    let worker!: WorkerLike & { posted: { id: number }[] }
    const hasher = new Hasher(() => {
      worker = { posted: [], onmessage: null, onerror: null, terminate() {}, postMessage(m: unknown) { this.posted.push(m as { id: number }) } }
      return worker
    })
    const first = hasher.hash(new Blob(['a']))
    const second = hasher.hash(new Blob(['b']))
    const say = (data: unknown) => worker.onmessage!({ data } as MessageEvent)
    say({ id: 1, type: 'done', sha256: 'x1', head: 'h1' })
    say({ id: 1, type: 'error', message: 'late failure of job 1' })
    say({ id: 2, type: 'done', sha256: 'x2', head: 'h2' })
    await expect(first).resolves.toEqual({ sha256: 'x1', head: 'h1' })
    await expect(second).resolves.toEqual({ sha256: 'x2', head: 'h2' })
    expect(worker.posted.map((m) => m.id)).toEqual([1, 2])
  })
})

describe('digest', () => {
  it('equals sha256 of the whole file across slice boundaries', async () => {
    const data = bytes(1024 * 1024 + 777, 3)
    const digest = await digestFile(new Blob([data]), undefined, 100_000)
    expect(digest.sha256).toBe(createHash('sha256').update(data).digest('hex'))
    expect(digest.head).toBe(createHash('sha256').update(data.slice(0, 1024 * 1024)).digest('hex'))
  })
})

describe('platform', () => {
  const doc = (url: string, accept: string) =>
    ({ querySelector: (q: string) => ({ getAttribute: () => (q.includes('app-url') ? url : accept) }) }) as unknown as Document
  it('tells an iPhone, an iPad and Android apart', () => {
    expect(isIOS('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)', 5)).toBe(true)
    expect(isIOS('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', 5)).toBe(true)
    expect(isIOS('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', 0)).toBe(false)
    expect(isIOS('Mozilla/5.0 (Linux; Android 15; Pixel 9)', 5)).toBe(false)
  })
  it('offers the app link only when the door filled in an https address', () => {
    expect(appUrl(doc('https://testflight.apple.com/join/x', ''))).toBe('https://testflight.apple.com/join/x')
    expect(appUrl(doc('', ''))).toBeNull()
    expect(appUrl(doc('__MEMOS_APP_URL__', ''))).toBeNull()
  })
  it('takes the accept list from the door, or none', () => {
    expect(acceptList(doc('', '.jpg,.heic,.mov'))).toBe('.jpg,.heic,.mov')
    expect(acceptList(doc('', '__MEMOS_ACCEPT__'))).toBe('')
    expect(extensionAccepted('IMG.HEIC', '.jpg,.heic')).toBe(true)
    expect(extensionAccepted('notes.txt', '.jpg,.heic')).toBe(false)
  })
})

describe('api', () => {
  it('sends no referrer, no cookies, and tells a validation 422 from a completion 422', async () => {
    const seen: RequestInit[] = []
    const api = createGuestApi(async (_url, init) => {
      seen.push(init)
      return new Response(JSON.stringify({ detail: [{ msg: 'x' }] }), { status: 422 })
    })
    const error = await api.complete('trip_dev_a', ['u1'], null).catch((e) => e)
    expect(error).toBeInstanceOf(GuestHttpError)
    expect(Array.isArray((error as GuestHttpError).detail)).toBe(true)
    expect(seen[0]).toMatchObject({ referrerPolicy: 'no-referrer', credentials: 'omit', cache: 'no-store' })
  })
})
