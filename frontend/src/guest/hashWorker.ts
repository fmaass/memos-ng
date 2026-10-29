import { digestFile } from './digest'

// Same-origin worker: hashes one File per message off the main thread.
interface Job {
  id: number
  file: Blob
}

self.onmessage = async (event: MessageEvent<Job>) => {
  const { id, file } = event.data
  try {
    const digest = await digestFile(file, (done) => self.postMessage({ id, type: 'progress', done }))
    self.postMessage({ id, type: 'done', ...digest })
  } catch (error) {
    self.postMessage({ id, type: 'error', message: String(error) })
  }
}
