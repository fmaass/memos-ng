// Durable upload bindings (MEMNG-1000): which server upload holds the bytes of which file, so a
// guest who reselects after a tab kill resumes instead of starting over. Namespaced by device
// (IndexedDB is already per origin), keyed by the FULL sha256. The candidate fields (name, size,
// lastModified, sha256 of the first MiB) only ever name a candidate; a binding is used only on a
// full-digest match.

export interface Binding {
  deviceId: string
  sha256: string
  uploadUid: string
  size: number
  offset: number
  state: 'open' | 'uploaded'
  name: string
  lastModified: number
  head: string
}

export interface PendingUpload {
  uid: string
  sha256: string
  size: number
}

/** A completion's upload set, persisted BEFORE its POST is sent and replayed first on reload. */
export interface PendingCompletion {
  deviceId: string
  uploads: PendingUpload[]
  caption: string | null
}

export interface BindingStore {
  get(deviceId: string, sha256: string): Promise<Binding | null>
  put(binding: Binding): Promise<void>
  delete(deviceId: string, sha256: string): Promise<void>
  list(deviceId: string): Promise<Binding[]>
  candidates(deviceId: string, name: string, size: number, lastModified: number, head: string): Promise<Binding[]>
  getPending(deviceId: string): Promise<PendingCompletion | null>
  putPending(pending: PendingCompletion): Promise<void>
  deletePending(deviceId: string): Promise<void>
}

const DB_NAME = 'memos-guest'
const BINDINGS = 'bindings'
const PENDING = 'completions'

function done<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

function committed(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error)
    tx.onabort = () => reject(tx.error)
  })
}

export async function openBindings(factory: IDBFactory = indexedDB): Promise<BindingStore> {
  const open = factory.open(DB_NAME, 1)
  open.onupgradeneeded = () => {
    const db = open.result
    const bindings = db.createObjectStore(BINDINGS, { keyPath: ['deviceId', 'sha256'] })
    bindings.createIndex('device', 'deviceId')
    bindings.createIndex('candidate', ['deviceId', 'name', 'size', 'lastModified', 'head'])
    db.createObjectStore(PENDING, { keyPath: 'deviceId' })
  }
  const db = await done(open)
  const write = async (store: string, act: (s: IDBObjectStore) => void) => {
    const tx = db.transaction(store, 'readwrite')
    act(tx.objectStore(store))
    await committed(tx)
  }
  const read = <T>(store: string, act: (s: IDBObjectStore) => IDBRequest<T>) =>
    done(act(db.transaction(store, 'readonly').objectStore(store)))
  return {
    async get(deviceId, sha256) {
      return ((await read(BINDINGS, (s) => s.get([deviceId, sha256]))) as Binding | undefined) ?? null
    },
    put: (binding) => write(BINDINGS, (s) => s.put(binding)),
    delete: (deviceId, sha256) => write(BINDINGS, (s) => s.delete([deviceId, sha256])),
    async list(deviceId) {
      return (await read(BINDINGS, (s) => s.index('device').getAll(deviceId))) as Binding[]
    },
    async candidates(deviceId, name, size, lastModified, head) {
      const key = [deviceId, name, size, lastModified, head]
      return (await read(BINDINGS, (s) => s.index('candidate').getAll(key))) as Binding[]
    },
    async getPending(deviceId) {
      return ((await read(PENDING, (s) => s.get(deviceId))) as PendingCompletion | undefined) ?? null
    },
    putPending: (pending) => write(PENDING, (s) => s.put(pending)),
    deletePending: (deviceId) => write(PENDING, (s) => s.delete(deviceId)),
  }
}

/** Used when IndexedDB is unavailable (a private window): uploads work, resume does not. */
export function memoryBindings(): BindingStore {
  const bindings = new Map<string, Binding>()
  const pending = new Map<string, PendingCompletion>()
  const key = (d: string, s: string) => `${d}\n${s}`
  const clone = <T>(v: T): T => structuredClone(v)
  return {
    get: async (d, s) => (bindings.has(key(d, s)) ? clone(bindings.get(key(d, s))!) : null),
    put: async (b) => void bindings.set(key(b.deviceId, b.sha256), clone(b)),
    delete: async (d, s) => void bindings.delete(key(d, s)),
    list: async (d) => [...bindings.values()].filter((b) => b.deviceId === d).map(clone),
    candidates: async (d, n, z, m, h) =>
      [...bindings.values()]
        .filter((b) => b.deviceId === d && b.name === n && b.size === z && b.lastModified === m && b.head === h)
        .map(clone),
    getPending: async (d) => (pending.has(d) ? clone(pending.get(d)!) : null),
    putPending: async (p) => void pending.set(p.deviceId, clone(p)),
    deletePending: async (d) => void pending.delete(d),
  }
}
