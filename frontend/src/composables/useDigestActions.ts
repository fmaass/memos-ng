// The digest's three ACTION endpoints — preview, test-send and run — and nothing else.
//
// **Wired to schema.d.ts after Phase 2 merges.** These three routes render or send, so they arrive
// with the pipeline in Phase 2 and are absent from `openapi.json` (and therefore from the generated
// `paths`) while this file is written. Everything else the Mail panel talks to is bound to the
// generated client in `useDigest.ts`; this module is the ONE place that is not, so re-binding it is
// a single edit: replace `request*` with `api.GET`/`api.POST` calls and delete the interfaces below
// in favour of `components['schemas'][…]`. Nothing else in the panel imports `fetch`.
//
// The request/response shapes below mirror the Phase-2 contract in
// `docs/plans/PLAN_2026-08-30_daily-digest.md` § "Phase 2 — Digest pipeline in the worker":
//   GET  /api/admin/digest/preview   -> {html, plain, memo_count, parts, selected[], deferred[]}
//   POST /api/admin/digest/test-send  body {"empty"?: true}; always sends, to the caller only
//   POST /api/admin/digest/run        -> the digest_runs row; 409 while a run is in flight
//
// Every shape one of them answers with is declared HERE, locally. The module imports no generated
// type at all — the isolation IS the contract, so a `components['schemas'][…]` reference would
// quietly couple it to a schema that does not describe these routes yet. The one thing it borrows
// is `apiErrorMessage`, a pure string helper with no type coupling.
import { ref } from 'vue'

import { redirectToLogin } from '@/api/client'
import { apiErrorMessage } from '@/lib/digest'

export interface DigestPreviewMemo {
  uid: string
  memo_id: number
  created_at: string
  local_day: string
}

export interface DigestPreviewDeferred {
  uid: string
  reason: string
}

/** The `digest_runs` row `POST /run` answers with — declared locally on purpose (see the header),
 * even though Phase 1 already publishes the same shape as `DigestRunOut`. */
export interface DigestRunRow {
  id: number
  trigger: string
  started_at: string
  finished_at: string | null
  status: string | null
  memos_sent: number
  parts: number
  parts_sent: number
  parts_claimed: number
  recipients: number
  error: string | null
  local_date: string
}

export interface DigestPreview {
  html: string
  plain: string
  memo_count: number
  parts: number
  selected: DigestPreviewMemo[]
  deferred: DigestPreviewDeferred[]
}

export const PREVIEW_PATH = '/api/admin/digest/preview'
export const TEST_SEND_PATH = '/api/admin/digest/test-send'
export const RUN_PATH = '/api/admin/digest/run'

export class DigestActionError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'DigestActionError'
  }
}

/** A same-origin call to one of the three action routes.
 *
 * `fetch` rather than the generated client only because these paths are not in the schema yet — the
 * 401 handling is the same redirect the client's middleware performs, borrowed from it rather than
 * re-implemented, so a dead session behaves identically here and everywhere else.
 */
async function call(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: init?.body ? { 'content-type': 'application/json' } : undefined,
    ...init,
  })
  if (response.status === 401) {
    redirectToLogin()
    throw new DigestActionError(401, 'The session has expired.')
  }
  if (!response.ok) {
    let body: unknown = null
    try {
      body = await response.json()
    } catch {
      body = null
    }
    throw new DigestActionError(response.status, apiErrorMessage(body, response.status))
  }
  return response
}

export function useDigestActions() {
  const preview = ref<DigestPreview | null>(null)
  const previewing = ref(false)
  const previewError = ref<string | null>(null)

  const sendingTest = ref(false)
  const runningNow = ref(false)
  const actionError = ref<string | null>(null)
  const actionNotice = ref<string | null>(null)

  /** Renders what would go out now. Serialized: a second click while one render is in flight is
   * refused, not queued — two renders would race to fill the same drawer. */
  async function loadPreview(): Promise<DigestPreview | null> {
    if (previewing.value) return null
    previewing.value = true
    previewError.value = null
    try {
      const response = await call(PREVIEW_PATH)
      const data = (await response.json()) as DigestPreview
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
  async function sendTest(empty = false): Promise<boolean> {
    if (sendingTest.value) return false
    sendingTest.value = true
    actionError.value = null
    try {
      await call(TEST_SEND_PATH, { method: 'POST', body: JSON.stringify(empty ? { empty: true } : {}) })
      actionNotice.value = empty
        ? 'An empty test mail is on its way to your address.'
        : 'A test mail is on its way to your address.'
      return true
    } catch (error) {
      actionError.value = error instanceof Error ? error.message : 'The test send failed.'
      return false
    } finally {
      sendingTest.value = false
    }
  }

  /** A real run, now. The API answers 409 while another run is in flight — that is a state, not a
   * fault, so it is reported as one rather than as a failed request. */
  async function runNow(): Promise<DigestRunRow | null> {
    if (runningNow.value) return null
    runningNow.value = true
    actionError.value = null
    try {
      const response = await call(RUN_PATH, { method: 'POST' })
      const data = (await response.json()) as DigestRunRow
      actionNotice.value = 'A run has started.'
      return data
    } catch (error) {
      actionError.value =
        error instanceof DigestActionError && error.status === 409
          ? 'A run is already in flight — wait for it to finish.'
          : error instanceof Error
            ? error.message
            : 'The run could not be started.'
      return null
    } finally {
      runningNow.value = false
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
    actionError,
    actionNotice,
    clearActionMessages,
    loadPreview,
    sendTest,
    runNow,
  }
}
