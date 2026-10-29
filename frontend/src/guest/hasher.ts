import type { Digest } from './digest'

// The main-thread side of the hash worker. Jobs run one at a time (memory stays one slice). A
// job's answer is ONE-SHOT: a worker can report `done` and then fail (or fail twice), so the
// first delivery claims the job synchronously, in the message callback, before anything awaits.

export interface WorkerLike {
  postMessage(message: unknown): void
  onmessage: ((event: MessageEvent) => void) | null
  onerror: ((event: Event) => void) | null
  terminate(): void
}

interface Pending {
  id: number
  file: Blob
  onProgress?: (done: number) => void
  resolve: (digest: Digest) => void
  reject: (error: Error) => void
}

export class Hasher {
  private worker: WorkerLike | null = null
  private queue: Pending[] = []
  private active: Pending | null = null
  private nextId = 1

  constructor(private readonly makeWorker: () => WorkerLike) {}

  hash(file: Blob, onProgress?: (done: number) => void): Promise<Digest> {
    return new Promise((resolve, reject) => {
      this.queue.push({ id: this.nextId++, file, onProgress, resolve, reject })
      this.next()
    })
  }

  private next(): void {
    if (this.active || this.queue.length === 0) return
    const job = this.queue.shift()!
    this.active = job
    const worker = this.ensure()
    worker.postMessage({ id: job.id, file: job.file })
  }

  /** Claims the active job if `id` is still it; the one place an answer is accepted. */
  private claim(id: number): Pending | null {
    const job = this.active
    if (!job || job.id !== id) return null
    this.active = null
    return job
  }

  private ensure(): WorkerLike {
    if (this.worker) return this.worker
    const worker = this.makeWorker()
    worker.onmessage = (event: MessageEvent) => {
      const data = event.data as { id: number; type: string; done?: number; sha256?: string; head?: string; message?: string }
      if (data.type === 'progress') {
        if (this.active?.id === data.id) this.active.onProgress?.(data.done ?? 0)
        return
      }
      const job = this.claim(data.id)
      if (!job) return // a second answer for a job already answered
      if (data.type === 'done') job.resolve({ sha256: data.sha256!, head: data.head! })
      else job.reject(new Error(data.message ?? 'hash failed'))
      this.next()
    }
    worker.onerror = () => {
      const job = this.active
      if (job) this.claim(job.id)?.reject(new Error('hash worker failed'))
      this.worker?.terminate()
      this.worker = null
      this.next()
    }
    this.worker = worker
    return worker
  }
}

export function browserHasher(): Hasher {
  return new Hasher(
    () => new Worker(new URL('./hashWorker.ts', import.meta.url), { type: 'module' }) as unknown as WorkerLike,
  )
}
