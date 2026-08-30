import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useDigestActions } from '@/composables/useDigestActions'
import type { DigestRun } from '@/composables/useDigestActions'

const { GET, POST } = vi.hoisted(() => ({ GET: vi.fn(), POST: vi.fn() }))
vi.mock('@/composables/useApi', () => ({ useApi: () => ({ GET, POST }) }))

const ok = (data: unknown, status = 200) => ({ data, error: undefined, response: { status } })
const fail = (detail: unknown, status: number) => ({
  data: undefined,
  error: { detail },
  response: { status },
})

const PREVIEW = {
  html: '<p>hello</p>',
  plain: 'hello',
  memo_count: 2,
  parts: 1,
  selected: [{ uid: 'abc', memo_id: 1, created_at: '2026-08-30T05:00:00Z', local_day: '2026-08-30' }],
  deferred: [{ uid: 'def', reason: 'card exceeds max_email_bytes after shedding' }],
}

function run(over: Partial<DigestRun> = {}): DigestRun {
  return {
    id: 12,
    trigger: 'manual',
    started_at: '2026-08-30T05:00:00Z',
    finished_at: null,
    status: null,
    memos_sent: 0,
    parts: 0,
    parts_sent: 0,
    parts_claimed: 0,
    recipients: 0,
    error: null,
    local_date: '2026-08-30',
    ...over,
  }
}

// No real backoff in a unit test — the delays are injected, and their LENGTH is still the budget.
const fast = (attempts = 3) => ({ pollDelays: new Array(attempts).fill(0) })

describe('useDigestActions', () => {
  beforeEach(() => {
    GET.mockReset()
    POST.mockReset()
  })
  afterEach(() => vi.clearAllMocks())

  it('renders a preview and keeps its selected and deferred lists', async () => {
    GET.mockResolvedValue(ok(PREVIEW))
    const actions = useDigestActions()

    const result = await actions.loadPreview()
    expect(GET).toHaveBeenCalledWith('/api/admin/digest/preview')
    expect(result?.memo_count).toBe(2)
    expect(actions.preview.value?.deferred[0].reason).toMatch(/max_email_bytes/)
    expect(actions.previewError.value).toBeNull()
  })

  it('refuses a second preview while one is still rendering', async () => {
    let release: (r: unknown) => void = () => {}
    GET.mockReturnValueOnce(new Promise((resolve) => (release = resolve)))
    const actions = useDigestActions()

    const first = actions.loadPreview()
    const second = await actions.loadPreview()
    release(ok(PREVIEW))

    expect(second).toBeNull()
    expect((await first)?.memo_count).toBe(2)
    expect(GET).toHaveBeenCalledTimes(1)
  })

  it("reports a failed preview with the API's own words and clears the stale render", async () => {
    GET.mockResolvedValue(fail('rendering failed', 500))
    const actions = useDigestActions()

    expect(await actions.loadPreview()).toBeNull()
    expect(actions.previewError.value).toBe('rendering failed')
    expect(actions.preview.value).toBeNull()
  })

  it('sends a test to the caller, and an explicitly empty one for the SMTP probe', async () => {
    POST.mockResolvedValue(ok({ sent_to: 'admin@example.test', memo_count: 3 }))
    const actions = useDigestActions()

    expect((await actions.sendTest(false))?.memo_count).toBe(3)
    expect(POST).toHaveBeenCalledWith('/api/admin/digest/test-send', { body: { empty: false } })
    expect(actions.actionNotice.value).toMatch(/3 memos/)

    await actions.sendTest(true)
    expect(POST).toHaveBeenLastCalledWith('/api/admin/digest/test-send', { body: { empty: true } })
    expect(actions.actionNotice.value).toMatch(/empty test mail/i)
  })

  it('reports a refused test send rather than claiming it went out', async () => {
    POST.mockResolvedValue(fail('SMTP is not configured', 422))
    const actions = useDigestActions()

    expect(await actions.sendTest()).toBeNull()
    expect(actions.actionError.value).toBe('SMTP is not configured')
    expect(actions.actionNotice.value).toBeNull()
  })

  it('claims a run, refreshes the table on the claim, then watches it to its terminal status', async () => {
    const onRunChanged = vi.fn()
    POST.mockResolvedValue(ok({ id: 12, status: null, trigger: 'manual' }, 202))
    GET.mockResolvedValueOnce(ok(run())) // still in flight
      .mockResolvedValueOnce(ok(run({ status: 'sent', memos_sent: 7, recipients: 2 })))
    const actions = useDigestActions({ onRunChanged, ...fast() })

    const claim = await actions.runNow()

    expect(claim?.id).toBe(12)
    expect(POST).toHaveBeenCalledWith('/api/admin/digest/run')
    expect(GET).toHaveBeenCalledWith('/api/admin/digest/runs/{run_id}', {
      params: { path: { run_id: 12 } },
    })
    expect(actions.actionNotice.value).toMatch(/7 memos to 2 recipients/)
    expect(actions.runningNow.value).toBe(false)
    expect(onRunChanged).toHaveBeenCalledTimes(2) // once on the claim, once when it landed
  })

  it("reports a run that ended badly with the row's own reason", async () => {
    POST.mockResolvedValue(ok({ id: 13, status: null, trigger: 'manual' }, 202))
    GET.mockResolvedValue(ok(run({ id: 13, status: 'failed', error: 'SMTP is not configured' })))
    const actions = useDigestActions(fast())

    await actions.runNow()
    expect(actions.actionError.value).toBe('The run failed: SMTP is not configured')
    expect(actions.actionNotice.value).toBeNull()
  })

  it('gives up after its budget without pretending the run failed', async () => {
    POST.mockResolvedValue(ok({ id: 14, status: null, trigger: 'manual' }, 202))
    GET.mockResolvedValue(ok(run({ id: 14 }))) // never leaves NULL
    const actions = useDigestActions(fast(3))

    await actions.runNow()
    expect(GET).toHaveBeenCalledTimes(3) // bounded by the delay list, not by the run
    expect(actions.actionNotice.value).toMatch(/still going/)
    expect(actions.actionError.value).toBeNull()
  })

  it('keeps watching through a transient read failure', async () => {
    POST.mockResolvedValue(ok({ id: 15, status: null, trigger: 'manual' }, 202))
    GET.mockResolvedValueOnce(fail('gateway timeout', 504)).mockResolvedValueOnce(
      ok(run({ id: 15, status: 'sent', memos_sent: 1, recipients: 1 })),
    )
    const actions = useDigestActions(fast())

    await actions.runNow()
    expect(actions.actionNotice.value).toMatch(/1 memo to 1 recipient/)
  })

  it('reports a 409 as a run already in flight, not as a fault, and watches nothing', async () => {
    POST.mockResolvedValue(fail('a run is already in flight', 409))
    const actions = useDigestActions(fast())

    expect(await actions.runNow()).toBeNull()
    expect(actions.actionError.value).toMatch(/already in flight/i)
    expect(GET).not.toHaveBeenCalled()
  })

  it('refuses a second run while one is still being watched', async () => {
    let release: (r: unknown) => void = () => {}
    POST.mockReturnValueOnce(new Promise((resolve) => (release = resolve)))
    GET.mockResolvedValue(ok(run({ status: 'sent' })))
    const actions = useDigestActions(fast())

    const first = actions.runNow()
    const second = await actions.runNow()
    release(ok({ id: 12, status: null, trigger: 'manual' }, 202))
    await first

    expect(second).toBeNull()
    expect(POST).toHaveBeenCalledTimes(1)
  })
})
