import { GuestHttpError, type CreateOut, type GuestApi, type Limits, type Receipt } from './api'
import type { BindingStore, PendingCompletion, PendingUpload } from './bindings'
import type { Credential, CredentialStore } from './credential'
import type { Digest } from './digest'

// The guest uploader (trip pass P4). Its own store — not the member useResumableUpload.
//
// Invariants, each pinned by a test in __tests__/uploader.spec.ts:
// - ONE in-flight create per content digest and ONE in-flight PATCH stream per upload uid: an
//   overlapping caller JOINS the operation in flight; it never starts a second one. The same
//   bytes selected twice therefore share one upload, sent once and completed once.
// - ONE completion in flight (complete() and the reload replay JOIN).
// - Every effect after an await is FENCED: the credential epoch that issued the request and the
//   uploader generation must both still be current, or the answer is dropped. An uploader belongs
//   to the credential it was built for: once another is current it starts nothing more.
// - A completion that settles re-judges files that finished while it was in flight; its retry
//   budget counts only rounds that settled nothing.
// - A completion receipt is accepted ONCE, claimed synchronously by batch id.
// - A refusal (401/410) clears the credential only if the credential that EARNED it is current.
// - After any interrupted or uncertain PATCH the next byte sent is at the server's HEAD offset.
// - A failed upload is identified by its UID (HEAD), never by a filename in an error detail.
// - A drop does not spend the queue: create and HEAD sit out a transient failure on one budget per
//   request, inside the one request's own slot, and time the browser reports offline spends none.

export type ItemState =
  | 'waiting'
  | 'hashing'
  | 'checking'
  | 'sending'
  | 'uploaded'
  | 'received'
  | 'unsupported'
  | 'too-large'
  | 'damaged'
  | 'quota'
  | 'failed'

export interface UploadItem {
  id: number
  file: File
  name: string
  size: number
  state: ItemState
  hashed: number
  sent: number
  sha256: string | null
  head: string | null
  uid: string | null
  recognised: boolean
  resumed: boolean
  retried: boolean
  message: string | null
}

export interface UploaderDeps {
  api: GuestApi
  credentials: CredentialStore
  bindings: BindingStore
  hash: (file: File, onProgress: (done: number) => void) => Promise<Digest>
  limits: Limits
  deviceId: string
  onChange?: () => void
  onRefused?: (status: number) => void
  onReceipt?: (receipt: Receipt) => void
  sleep?: (ms: number) => Promise<void>
  network?: Connectivity
  parallel?: number
  patchTimeoutMs?: number
}

/** What the browser believes about its connection. A hint, wrong in both directions at times: it
 * may only shorten a wait or skip spending budget, never decide that a request is not sent. */
export interface Connectivity {
  online(): boolean
  /** Subscribes to the browser coming back online; answers the unsubscribe. */
  onOnline(listener: () => void): () => void
}

export const browserNetwork: Connectivity = {
  online: () => typeof navigator === 'undefined' || navigator.onLine !== false,
  onOnline: (listener) => {
    window.addEventListener('online', listener)
    return () => window.removeEventListener('online', listener)
  },
}

interface Fence {
  credential: Credential
  generation: number
}

type SendOutcome = 'done' | 'gone' | 'stopped'

const ACTIVE: ItemState[] = ['waiting', 'hashing', 'checking', 'sending']
const MAX_FAILURES = 8
const DEFAULT_SLICE = 5 * 1024 * 1024

const backoff = (n: number) => Math.min(30_000, 1000 * 2 ** Math.max(0, n - 1))

// What one request's whole budget adds up to (121 s). A Retry-After beyond it is no blip to sit out
// but a statement about the hours ahead: the door's file caps answer create with 3600 s
// (FILE_CAP_RETRY_AFTER_S in app/ingest/guest.py), and holding a worker on "Checking" for that long
// would hide the door's own sentence from the guest. So it fails now, and a Retry may ask again.
const RETRY_BUDGET_MS = Array.from({ length: MAX_FAILURES }, (_, n) => backoff(n + 1)).reduce((a, b) => a + b)

/** Worth asking again: no answer at all, a busy door, or a server fault. 507 is a 5xx by number
 * only: "no room" is a verdict with its own state. */
const isTransient = (error: unknown): boolean =>
  !(error instanceof GuestHttpError) || error.status === 429 || (error.status >= 500 && error.status !== 507)

export class GuestUploader {
  readonly items: UploadItem[] = []
  private generation = 0
  private nextId = 1
  private running = 0
  private ready: Promise<void> = Promise.resolve()
  private readonly creating = new Map<string, Promise<CreateOut>>()
  private readonly sending = new Map<string, Promise<SendOutcome>>()
  /** Uploads whose last PATCH has no definite answer: whatever resumes them next (the loop, a
   * manual retry) asks HEAD first, never the page's own offset. */
  private readonly uncertain = new Set<string>()
  private completing: Promise<void> | null = null
  private readonly handledReceipts = new Set<string>()
  /** The uids the last completion left unsettled (its budget spent), so they are not resubmitted
   * in a loop; anything else that is uploaded is judged after a completion settles. */
  private unsettled = new Set<string>()
  private readonly sleep: (ms: number) => Promise<void>
  private readonly network: Connectivity
  /** The credential this uploader was built for. Its device namespace and every request belong
   * to it; once another credential is current this uploader starts nothing more. */
  private readonly origin: Credential | null

  constructor(private readonly deps: UploaderDeps) {
    this.sleep = deps.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)))
    this.network = deps.network ?? browserNetwork
    this.origin = deps.credentials.get()
  }

  get inFlight(): boolean {
    return this.completing !== null || this.items.some((i) => ACTIVE.includes(i.state))
  }

  get receivedCount(): number {
    return this.items.filter((i) => i.state === 'received').length
  }

  private fence(): Fence | null {
    const origin = this.origin
    return origin && this.deps.credentials.isCurrent(origin) ? { credential: origin, generation: this.generation } : null
  }

  private alive(f: Fence): boolean {
    return f.generation === this.generation && this.deps.credentials.isCurrent(f.credential)
  }

  private changed(): void {
    this.deps.onChange?.()
  }

  /** Abandons everything in flight: no answer to an older generation lands anywhere. */
  reset(): void {
    this.generation++
    this.running = 0
    this.items.splice(0)
    this.creating.clear()
    this.sending.clear()
    this.completing = null
    this.changed()
  }

  /** Replays a persisted completion before any file work, so an upload the replay finishes is
   * never read as lost from a HEAD 404. */
  resumePending(): Promise<void> {
    const f = this.fence()
    if (!f) return Promise.resolve()
    this.ready = this.joinComplete(() => this.replay(f))
    return this.ready
  }

  add(files: File[]): void {
    for (const file of files) {
      const item: UploadItem = {
        id: this.nextId++,
        file,
        name: file.name,
        size: file.size,
        state: 'waiting',
        hashed: 0,
        sent: 0,
        sha256: null,
        head: null,
        uid: null,
        recognised: false,
        resumed: false,
        retried: false,
        message: null,
      }
      // Checked before any hashing: a file the door would refuse costs the guest nothing.
      if (file.size > this.deps.limits.max_file_bytes) item.state = 'too-large'
      if (file.size === 0) item.state = 'unsupported'
      this.items.push(item)
    }
    this.changed()
    this.pump()
  }

  retry(item: UploadItem): void {
    if (item.state !== 'failed') return
    item.state = 'waiting'
    item.message = null
    this.pump()
    this.changed() // a create that now sits out a drop answers nothing for a while
  }

  private pump(): void {
    while (this.running < (this.deps.parallel ?? 2)) {
      const f = this.fence()
      const item = this.items.find((i) => i.state === 'waiting')
      if (!f || !item) break
      item.state = item.sha256 ? 'checking' : 'hashing' // claimed synchronously
      this.running++
      void this.process(item, f).finally(() => {
        if (f.generation !== this.generation) return
        this.running--
        this.changed()
        this.pump()
        this.maybeComplete()
      })
    }
  }

  private maybeComplete(): void {
    if (this.completing || this.items.some((i) => ACTIVE.includes(i.state))) return
    if (this.items.some((i) => i.state === 'uploaded')) void this.complete()
  }

  private async process(item: UploadItem, f: Fence): Promise<void> {
    await this.ready
    if (!this.alive(f)) return
    const { bindings, deviceId } = this.deps
    try {
      if (!item.sha256) {
        item.state = 'hashing'
        const digest = await this.deps.hash(item.file, (done) => {
          if (!this.alive(f)) return
          item.hashed = done
          this.changed()
        })
        if (!this.alive(f)) return
        item.sha256 = digest.sha256
        item.head = digest.head
      }
      item.state = 'checking'
      const lastModified = item.file.lastModified
      const candidates = await bindings.candidates(deviceId, item.name, item.size, lastModified, item.head!)
      if (!this.alive(f)) return
      item.recognised = candidates.length > 0 // a hint for the guest; the full digest decides
      for (let attempt = 0; attempt < 3; attempt++) {
        if (!item.uid) {
          const binding = await bindings.get(deviceId, item.sha256)
          if (!this.alive(f)) return
          if (binding) {
            const offset = await this.persisting(f, () => this.deps.api.head(f.credential.token, binding.uploadUid))
            if (!this.alive(f)) return
            if (offset === null) {
              await bindings.delete(deviceId, item.sha256)
              if (!this.alive(f)) return
            } else {
              item.uid = binding.uploadUid
              item.sent = offset
              item.resumed = true
            }
          }
        }
        if (!item.uid) {
          const created = await this.create(item, f)
          if (!this.alive(f)) return
          if (created.already_received || !created.upload_uid) {
            item.state = 'received' // on pluto already: never PATCHed, never in a completion
            item.message = 'already received'
            return
          }
          item.uid = created.upload_uid
          item.sent = created.bytes_received
          await this.bind(item, f)
          if (!this.alive(f)) return
        }
        const outcome = await this.send(item, f)
        if (outcome === 'done' && this.alive(f)) {
          item.sent = item.size // a joined twin shares the owner's finished stream
          item.state = 'uploaded'
        }
        if (outcome !== 'gone') return
        this.creating.delete(item.sha256)
        item.uid = null // the server no longer holds it: one fresh create
        item.sent = 0
      }
      item.state = 'failed'
    } catch (error) {
      this.fail(item, f, error)
    }
  }

  private create(item: UploadItem, f: Fence): Promise<CreateOut> {
    const key = item.sha256!
    const joined = this.creating.get(key)
    if (joined) return joined
    // The retries live inside the stored promise, so a twin joins the whole loop and never starts a
    // second create. Its fence is every joiner's: this map only holds one generation's creates
    // (reset() clears it) and all of them share the uploader's one credential.
    const created = this.persisting(f, () =>
      this.deps.api.create(f.credential.token, {
        filename: item.name,
        mime: item.file.type,
        size: item.size,
        last_modified: item.file.lastModified || null,
        sha256: key,
      }),
    )
    // Kept after it answers: the same bytes picked again later in this generation reuse the
    // upload rather than open a second one. Forgotten on failure and when the upload is gone.
    created.catch(() => {
      if (this.creating.get(key) === created) this.creating.delete(key)
    })
    this.creating.set(key, created)
    return created
  }

  /** Repeats one request through transient failures until it answers. Rejects when it cannot go on
   * (see sitOut); a rejection under a fallen fence lands in fail(), which drops it. */
  private async persisting<T>(f: Fence, request: () => Promise<T>): Promise<T> {
    let failures = 0
    for (;;) {
      try {
        return await request()
      } catch (error) {
        failures = await this.sitOut(error, failures, f)
      }
    }
  }

  /** Waits out one failed try and answers the failures counted so far. Throws the error instead
   * when it is a verdict, the budget is spent, or the fence fell: nothing follows a fallen fence. */
  private async sitOut(error: unknown, failures: number, f: Fence): Promise<number> {
    if (!this.alive(f) || !isTransient(error)) throw error
    if (await this.offlinePause(error)) {
      if (!this.alive(f)) throw error
      return failures
    }
    const spent = failures + 1
    const asked = error instanceof GuestHttpError && error.retryAfter ? error.retryAfter * 1000 : null
    if (spent > MAX_FAILURES || (asked ?? 0) > RETRY_BUDGET_MS) throw error
    await this.sleep(asked ?? backoff(spent))
    if (!this.alive(f)) throw error
    return spent
  }

  /** A request that got no answer while the browser reports offline waits for the network, at no
   * cost to any budget; answers whether it waited. An HTTP answer proves the network, and a request
   * is always tried first, so a browser wrongly saying offline delays nothing that can go. */
  private async offlinePause(error: unknown): Promise<boolean> {
    if (error instanceof GuestHttpError || this.network.online()) return false
    await new Promise<void>((resolve) => {
      const done = () => {
        off()
        resolve()
      }
      const off = this.network.onOnline(done)
      // Bounded: the event may never fire even though the network is back.
      void this.sleep(backoff(MAX_FAILURES)).then(done)
    })
    return true
  }

  private async bind(item: UploadItem, f: Fence): Promise<void> {
    if (!this.alive(f) || !item.uid || !item.sha256) return
    await this.deps.bindings.put({
      deviceId: this.deps.deviceId,
      sha256: item.sha256,
      uploadUid: item.uid,
      size: item.size,
      offset: item.sent,
      state: item.sent >= item.size ? 'uploaded' : 'open',
      name: item.name,
      lastModified: item.file.lastModified,
      head: item.head ?? '',
    })
  }

  private send(item: UploadItem, f: Fence): Promise<SendOutcome> {
    const uid = item.uid!
    const joined = this.sending.get(uid)
    if (joined) return joined
    const sent = this.sendLoop(item, uid, f).finally(() => {
      if (this.sending.get(uid) === sent) this.sending.delete(uid)
    })
    this.sending.set(uid, sent)
    return sent
  }

  private sliceBytes(): number {
    const { chunk_size, max_patch_bytes } = this.deps.limits
    const bounds = [chunk_size, max_patch_bytes].filter((n) => n > 0)
    return bounds.length ? Math.min(...bounds) : DEFAULT_SLICE
  }

  private async sendLoop(item: UploadItem, uid: string, f: Fence): Promise<SendOutcome> {
    const { api, bindings, deviceId } = this.deps
    const token = f.credential.token
    const slice = this.sliceBytes()
    let offset = item.sent
    let failures = 0
    let resync = this.uncertain.has(uid)
    item.state = 'sending'
    this.changed()
    while (resync || offset < item.size) {
      if (resync) {
        // After an interrupted or uncertain PATCH the server's committed offset is the only truth.
        try {
          const head = await api.head(token, uid)
          if (!this.alive(f)) return 'stopped'
          if (head === null) {
            await bindings.delete(deviceId, item.sha256!)
            return 'gone'
          }
          offset = head
          resync = false
          this.uncertain.delete(uid)
        } catch (error) {
          failures = await this.sitOut(error, failures, f)
          continue
        }
      } else {
        const end = Math.min(item.size, offset + slice)
        this.uncertain.add(uid) // until this PATCH answers with an offset
        try {
          const signal = AbortSignal.timeout(this.deps.patchTimeoutMs ?? 300_000)
          const next = await api.patch(token, uid, offset, item.file.slice(offset, end), signal)
          if (!this.alive(f)) return 'stopped'
          if (!Number.isFinite(next)) throw new Error('no Upload-Offset')
          offset = next
          failures = 0
          this.uncertain.delete(uid)
        } catch (error) {
          if (!this.alive(f)) return 'stopped'
          if (await this.offlinePause(error)) {
            if (!this.alive(f)) return 'stopped'
            resync = true
            continue
          }
          if (error instanceof GuestHttpError) {
            if (error.status === 404) {
              await bindings.delete(deviceId, item.sha256!)
              return 'gone'
            }
            if (error.status === 429) await this.sleep((error.retryAfter ?? 5) * 1000)
            else if (error.status !== 409 && error.status < 500) throw error
          }
          if (++failures > MAX_FAILURES) throw error
          if (!(error instanceof GuestHttpError) || error.status >= 500) await this.sleep(backoff(failures))
          if (!this.alive(f)) return 'stopped'
          resync = true
          continue
        }
      }
      item.sent = offset
      await this.bind(item, f)
      if (!this.alive(f)) return 'stopped'
      this.changed()
    }
    item.state = 'uploaded'
    return 'done'
  }

  private fail(item: UploadItem, f: Fence, error: unknown): void {
    if (!this.alive(f)) return
    if (error instanceof GuestHttpError) {
      if (error.status === 401 || error.status === 410) {
        item.state = 'waiting'
        this.refused(f, error.status)
        return
      }
      if (error.status === 415) item.state = 'unsupported'
      else if (error.status === 413) item.state = 'too-large'
      else if (error.status === 507) item.state = 'quota'
      else item.state = 'failed'
      item.message = typeof error.detail === 'string' ? error.detail : null
      return
    }
    item.state = 'failed'
    item.message = 'The connection was lost'
  }

  private refused(f: Fence, status: number): void {
    // Judged against the credential that EARNED the refusal, synchronously, right here.
    if (!this.deps.credentials.clearIfCurrent(f.credential)) return
    this.generation++
    this.running = 0
    this.deps.onRefused?.(status)
  }

  // --- completion --------------------------------------------------------------------------

  complete(caption: string | null = null): Promise<void> {
    return this.joinComplete(() => this.submitNew(caption))
  }

  private joinComplete(run: () => Promise<void>): Promise<void> {
    if (this.completing) return this.completing
    const running = run().finally(() => {
      if (this.completing === running) this.completing = null
      this.changed()
      this.pump()
      // Files that finished while this completion was in flight were declined by maybeComplete;
      // judge them now — except the set this completion left unsettled, which is never
      // resubmitted in a loop.
      const unclaimed = this.items.some((i) => i.state === 'uploaded' && i.uid && !this.unsettled.has(i.uid))
      if (unclaimed && this.fence()) this.maybeComplete()
    })
    this.completing = running
    return running
  }

  private async submitNew(caption: string | null): Promise<void> {
    await this.ready
    const f = this.fence()
    if (!f) return
    const seen = new Set<string>()
    const uploads: PendingUpload[] = []
    for (const i of this.items) {
      if (i.state !== 'uploaded' || !i.uid || seen.has(i.uid)) continue
      seen.add(i.uid)
      uploads.push({ uid: i.uid, sha256: i.sha256!, size: i.size })
    }
    if (!uploads.length) return
    const pending: PendingCompletion = { deviceId: this.deps.deviceId, uploads, caption }
    // Durable BEFORE the request leaves: a reload replays exactly this set first.
    await this.deps.bindings.putPending(pending)
    if (!this.alive(f)) return
    await this.submit(pending, f)
  }

  private async replay(f: Fence): Promise<void> {
    const pending = await this.deps.bindings.getPending(this.deps.deviceId)
    if (!pending || !this.alive(f)) return
    await this.submit(pending, f)
  }

  private async submit(start: PendingCompletion, f: Fence): Promise<void> {
    let pending: PendingCompletion | null = start
    try {
      // The budget counts rounds that settled nothing (a lost response, a set that did not shrink);
      // a round that dropped an upload from the set is progress, and the shrinking set bounds those.
      let stalled = 0
      while (pending && pending.uploads.length && stalled < 6) {
        let receipt: Receipt
        try {
          receipt = await this.deps.api.complete(
            f.credential.token,
            pending.uploads.map((u) => u.uid),
            pending.caption,
          )
        } catch (error) {
          if (!this.alive(f)) return
          if (await this.offlinePause(error)) {
            if (!this.alive(f)) return
            continue
          }
          if (!(error instanceof GuestHttpError)) {
            stalled++
            await this.sleep(backoff(stalled)) // response lost: replay the SAME set
            if (!this.alive(f)) return
            continue
          }
          if (error.status === 401 || error.status === 410) return this.refused(f, error.status)
          if (error.status === 507) return this.markSet(pending, 'quota')
          let cause: 'damaged' | 'unsupported' | 'vanished' | 'short'
          if (error.status === 422 && typeof error.detail === 'string') cause = 'damaged'
          else if (error.status === 415) cause = 'unsupported'
          else if (error.status === 409) cause = String(error.detail).includes('not fully') ? 'short' : 'vanished'
          else return this.markSet(pending, 'failed') // a request-validation 422 (list detail) and the rest
          const before = pending.uploads.length
          pending = await this.reconcile(pending, cause, f)
          if (pending && pending.uploads.length >= before) stalled++
          continue
        }
        if (!this.alive(f)) return
        await this.accept(receipt, pending, f)
        return
      }
      // The budget is spent with the set still unsettled: hand it back to the guest as failed,
      // with a Retry, rather than leave it "Sent, confirming" with nothing left to try it.
      if (pending && pending.uploads.length && this.alive(f)) {
        this.markSet(pending, 'failed', 'Not confirmed yet. Retry to send it again.')
      }
    } finally {
      this.unsettled = new Set(pending?.uploads.map((u) => u.uid) ?? [])
    }
  }

  private markSet(pending: PendingCompletion, state: ItemState, message: string | null = null): void {
    const uids = new Set(pending.uploads.map((u) => u.uid))
    for (const i of this.items) {
      if (!i.uid || !uids.has(i.uid)) continue
      i.state = state
      if (message) i.message = message
    }
  }

  /** Re-judges every upload of a refused completion BY UID: 200 = survivor, 404 = gone. The
   * revised set is persisted before the completion is retried with the survivors. */
  private async reconcile(
    pending: PendingCompletion,
    cause: 'damaged' | 'unsupported' | 'vanished' | 'short',
    f: Fence,
  ): Promise<PendingCompletion | null> {
    const { api, bindings, deviceId } = this.deps
    const survivors: PendingUpload[] = []
    for (const upload of pending.uploads) {
      let offset: number | null
      try {
        offset = await api.head(f.credential.token, upload.uid)
      } catch (error) {
        if (!this.alive(f)) return null
        if (error instanceof GuestHttpError && (error.status === 401 || error.status === 410)) {
          this.refused(f, error.status)
          return null
        }
        survivors.push(upload) // unknown for now: kept, judged again on the next round
        continue
      }
      if (!this.alive(f)) return null
      // EVERY item bound to this upload (the same bytes picked twice share one uid) is judged
      // together: a twin left holding a discarded uid would complete into a 409 and upload again.
      const twins = this.items.filter((i) => i.uid === upload.uid)
      if (offset === null) {
        this.creating.delete(upload.sha256)
        await bindings.delete(deviceId, upload.sha256)
        if (!this.alive(f)) return null
        for (const item of twins) {
          item.uid = null
          item.sent = 0
          if (cause === 'unsupported') item.state = 'unsupported'
          else if (item.retried) item.state = 'damaged'
          else {
            item.retried = true // ONE fresh upload, re-hashed first (twins join it again)
            item.sha256 = null
            item.state = 'waiting'
          }
        }
      } else if (offset < upload.size) {
        for (const item of twins) {
          item.sent = offset
          item.state = 'waiting' // resumed from its HEAD offset, completed with a later set
        }
      } else {
        survivors.push(upload)
      }
    }
    const revised: PendingCompletion = { ...pending, uploads: survivors }
    if (survivors.length) await bindings.putPending(revised)
    else await bindings.deletePending(deviceId)
    if (!this.alive(f)) return null
    this.changed()
    return revised
  }

  private async accept(receipt: Receipt, pending: PendingCompletion, f: Fence): Promise<void> {
    // One-shot: claimed synchronously, before any await, by the batch it names.
    if (this.handledReceipts.has(receipt.batch_id)) return
    this.handledReceipts.add(receipt.batch_id)
    const uids = new Set(pending.uploads.map((u) => u.uid))
    for (const i of this.items) if (i.uid && uids.has(i.uid)) i.state = 'received'
    this.deps.onReceipt?.(receipt)
    this.changed()
    await this.deps.bindings.deletePending(this.deps.deviceId)
    for (const u of pending.uploads) {
      if (!this.alive(f)) return
      await this.deps.bindings.delete(this.deps.deviceId, u.sha256)
    }
  }
}
