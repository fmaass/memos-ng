// The digest's three ACTION endpoints — preview, test-send and run — and nothing else.
//
// They render or send, so they keep their own module: a click here has an effect on the world,
// while `useDigest.ts` only reads and writes state. Every shape below comes from the generated
// schema; nothing here is hand-typed.
//
//   GET  /api/admin/digest/preview    -> DigestPreviewOut (part one, plus selected/deferred)
//   POST /api/admin/digest/test-send  -> DigestTestSendOut; body {empty} for the SMTP probe
//   POST /api/admin/digest/run        -> 202 DigestRunClaimOut + Location; the WORKER runs it
import { ref } from 'vue'

import type { components } from '@/api/schema'
import { useApi } from '@/composables/useApi'
import { apiErrorMessage } from '@/lib/digest'

export type DigestPreview = components['schemas']['DigestPreviewOut']
export type DigestRunClaim = components['schemas']['DigestRunClaimOut']
export type DigestTestSend = components['schemas']['DigestTestSendOut']
export type DigestRun = components['schemas']['DigestRunOut']

/** How long the UI waits for a claimed run to reach a terminal status, and how it backs off while
 * it waits. Bounded on purpose: `POST /run` only CLAIMS the row — the worker executes it, so a
 * worker that is down or busy must leave the page usable and honest rather than spinning forever.
 * The Runs table is the durable answer either way. */
export const RUN_POLL_DELAYS_MS = [500, 1000, 2000, 3000, 5000, 5000, 5000, 5000, 5000, 5000]

export interface DigestActionOptions {
  /** Called when the run's row changes: once on the claim, once when the poll settles. The panel
   * refreshes the Runs table with it. */
  onRunChanged?: () => void | Promise<void>
  /** Overridable so a test does not sleep through the real backoff. */
  pollDelays?: readonly number[]
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

export class DigestActionError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'DigestActionError'
  }
}

/** The answer, or a thrown `DigestActionError` carrying its status.
 *
 * openapi-fetch hands back `{data, error, response}` and TypeScript narrows `error` to `never` for
 * an operation whose only DECLARED response is 2xx — but the server still answers 403 on a
 * non-admin session and 409 when a run is already in flight. Reading through one loose shape keeps
 * the runtime check alive under that narrowing instead of letting the types delete it.
 */
function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error !== undefined || result.data === undefined) {
    throw new DigestActionError(
      result.response.status,
      apiErrorMessage(result.error, result.response.status),
    )
  }
  return result.data
}

export function useDigestActions(options: DigestActionOptions = {}) {
  const api = useApi()
  const pollDelays = options.pollDelays ?? RUN_POLL_DELAYS_MS

  const preview = ref<DigestPreview | null>(null)
  const previewing = ref(false)
  const previewError = ref<string | null>(null)

  const sendingTest = ref(false)
  const runningNow = ref(false)
  /** The claimed run while it is being watched — null once it lands or is given up on. */
  const watchedRun = ref<DigestRun | DigestRunClaim | null>(null)
  const actionError = ref<string | null>(null)
  const actionNotice = ref<string | null>(null)

  /** Renders what would go out now. Serialized: a second click while one render is in flight is
   * refused, not queued — two renders would race to fill the same drawer. */
  async function loadPreview(): Promise<DigestPreview | null> {
    if (previewing.value) return null
    previewing.value = true
    previewError.value = null
    try {
      const data = unwrap(await api.GET('/api/admin/digest/preview'))
      preview.value = data
      return data
    } catch (error) {
      previewError.value = error instanceof Error ? error.message : 'The preview failed.'
      preview.value = null
      return null
    } finally {
      previewing.value = false
    }
  }

  /** One real mail to the calling admin's own address. `empty` asks for the "no memos selected"
   * body without consulting the selection at all — the SMTP probe, which sends nothing else. */
  async function sendTest(empty = false): Promise<DigestTestSend | null> {
    if (sendingTest.value) return null
    sendingTest.value = true
    actionError.value = null
    try {
      const data = unwrap(await api.POST('/api/admin/digest/test-send', { body: { empty } }))
      actionNotice.value = empty
        ? 'An empty test mail went out to your address.'
        : `A test mail with ${data.memo_count} memo${data.memo_count === 1 ? '' : 's'} went out to your address.`
      return data
    } catch (error) {
      actionError.value = error instanceof Error ? error.message : 'The test send failed.'
      return null
    } finally {
      sendingTest.value = false
    }
  }

  /** Watch a claimed run until it reaches a terminal status or the budget runs out.
   *
   * `status === null` IS "in flight" in this API — the row is written once, terminally — so the
   * poll ends on the first non-null status and says what it was. Giving up is not a failure: the
   * run is still the worker's, and the Runs table keeps showing it.
   */
  async function watchRun(id: number): Promise<void> {
    for (const delay of pollDelays) {
      await sleep(delay)
      let data: DigestRun
      try {
        data = unwrap(
          await api.GET('/api/admin/digest/runs/{run_id}', { params: { path: { run_id: id } } }),
        )
      } catch {
        continue // a transient read must not end the watch; the budget still bounds it
      }
      watchedRun.value = data
      if (data.status !== null) {
        if (data.status === 'sent') {
          actionNotice.value = `The run finished: ${data.memos_sent} memo${
            data.memos_sent === 1 ? '' : 's'
          } to ${data.recipients} recipient${data.recipients === 1 ? '' : 's'}.`
        } else {
          actionNotice.value = null
          actionError.value = data.error
            ? `The run ${data.status}: ${data.error}`
            : `The run ${data.status}.`
        }
        return
      }
    }
    actionNotice.value = 'The run is still going — the Runs table shows how it ends.'
  }

  /** Claim a run, now. The API answers 202 with the claimed row and a Location: the work happens
   * in the worker, so this returns as soon as the row exists and then watches it. A 409 means
   * another run already holds the single-flight index — a state, not a fault.
   */
  async function runNow(): Promise<DigestRunClaim | null> {
    if (runningNow.value) return null
    runningNow.value = true
    actionError.value = null
    actionNotice.value = null
    watchedRun.value = null
    try {
      let data: DigestRunClaim
      try {
        data = unwrap(await api.POST('/api/admin/digest/run'))
      } catch (error) {
        actionError.value =
          error instanceof DigestActionError && error.status === 409
            ? 'A run is already in flight — wait for it to finish.'
            : error instanceof Error
              ? error.message
              : 'The run could not be started.'
        return null
      }
      watchedRun.value = data
      actionNotice.value = 'A run was claimed — waiting for the worker to finish it.'
      await options.onRunChanged?.() // the claimed row is already in the table, as "running"
      await watchRun(data.id)
      return data
    } finally {
      runningNow.value = false
      void options.onRunChanged?.()
    }
  }

  function clearActionMessages() {
    actionError.value = null
    actionNotice.value = null
  }

  return {
    preview,
    previewing,
    previewError,
    sendingTest,
    runningNow,
    watchedRun,
    actionError,
    actionNotice,
    clearActionMessages,
    loadPreview,
    sendTest,
    runNow,
  }
}
