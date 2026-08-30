import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  PREVIEW_PATH,
  RUN_PATH,
  TEST_SEND_PATH,
  useDigestActions,
} from '@/composables/useDigestActions'

const { redirectToLogin } = vi.hoisted(() => ({ redirectToLogin: vi.fn() }))
vi.mock('@/api/client', () => ({ redirectToLogin }))

function json(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

const PREVIEW = {
  html: '<p>hello</p>',
  plain: 'hello',
  memo_count: 2,
  parts: 1,
  selected: [{ uid: 'abc', memo_id: 1, created_at: '2026-08-30T05:00:00Z', local_day: '2026-08-30' }],
  deferred: [{ uid: 'def', reason: 'card exceeds max_email_bytes after shedding' }],
}

describe('useDigestActions', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
  })

  it('renders a preview and keeps its selected and deferred lists', async () => {
    vi.mocked(fetch).mockResolvedValue(json(PREVIEW))
    const actions = useDigestActions()

    const result = await actions.loadPreview()
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(PREVIEW_PATH)
    expect(result?.memo_count).toBe(2)
    expect(actions.preview.value?.deferred[0].reason).toMatch(/max_email_bytes/)
    expect(actions.previewError.value).toBeNull()
  })

  it('refuses a second preview while one is still rendering', async () => {
    let release: (r: Response) => void = () => {}
    vi.mocked(fetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        release = resolve
      }),
    )
    const actions = useDigestActions()

    const first = actions.loadPreview()
    const second = await actions.loadPreview()
    release(json(PREVIEW))

    expect(second).toBeNull()
    expect((await first)?.memo_count).toBe(2)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('reports a failed preview with the API\'s own words and clears the stale render', async () => {
    vi.mocked(fetch).mockResolvedValue(json({ detail: 'rendering failed' }, 500))
    const actions = useDigestActions()

    expect(await actions.loadPreview()).toBeNull()
    expect(actions.previewError.value).toBe('rendering failed')
    expect(actions.preview.value).toBeNull()
  })

  it('sends a test to the caller, and an explicitly empty one for the SMTP probe', async () => {
    vi.mocked(fetch).mockResolvedValue(json({}))
    const actions = useDigestActions()

    expect(await actions.sendTest(false)).toBe(true)
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(TEST_SEND_PATH)
    expect(vi.mocked(fetch).mock.calls[0][1]).toMatchObject({ method: 'POST', body: '{}' })

    expect(await actions.sendTest(true)).toBe(true)
    expect(vi.mocked(fetch).mock.calls[1][1]).toMatchObject({ body: '{"empty":true}' })
    expect(actions.actionNotice.value).toMatch(/empty test mail/i)
  })

  it('starts a run and reports a 409 as a run already in flight, not as a fault', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(json({ id: 12, status: null }))
    const actions = useDigestActions()

    expect((await actions.runNow())?.id).toBe(12)
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(RUN_PATH)
    expect(actions.actionNotice.value).toMatch(/run has started/i)

    vi.mocked(fetch).mockResolvedValueOnce(json({ detail: 'a run is already in flight' }, 409))
    expect(await actions.runNow()).toBeNull()
    expect(actions.actionError.value).toMatch(/already in flight/i)
  })

  it('bounces a dead session to the login the way the API client does', async () => {
    vi.mocked(fetch).mockResolvedValue(json({ detail: 'unauthenticated' }, 401))
    const actions = useDigestActions()

    expect(await actions.sendTest()).toBe(false)
    expect(redirectToLogin).toHaveBeenCalledTimes(1)
    expect(actions.actionError.value).toMatch(/session has expired/i)
  })

  it('refuses a second run and a second test while one is on the wire', async () => {
    let release: (r: Response) => void = () => {}
    vi.mocked(fetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        release = resolve
      }),
    )
    const actions = useDigestActions()

    const first = actions.sendTest()
    const second = await actions.sendTest()
    release(json({}))

    expect(second).toBe(false)
    expect(await first).toBe(true)
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
