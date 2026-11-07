import { type Ref, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RUNS_LIMIT, useDigest } from '@/composables/useDigest'
import type { DigestSettings } from '@/lib/digest'

const { GET, PUT, POST, PATCH, DELETE, setQueryData, invalidateQueries, state } = vi.hoisted(() => ({
  GET: vi.fn(),
  PUT: vi.fn(),
  POST: vi.fn(),
  PATCH: vi.fn(),
  DELETE: vi.fn(),
  setQueryData: vi.fn(),
  invalidateQueries: vi.fn(),
  state: {
    queries: [] as { queryKey: readonly unknown[]; queryFn: () => Promise<unknown> }[],
    data: new Map<string, Ref<unknown>>(),
  },
}))

vi.mock('@/composables/useApi', () => ({ useApi: () => ({ GET, PUT, POST, PATCH, DELETE }) }))
vi.mock('@tanstack/vue-query', () => ({
  useQueryClient: () => ({ setQueryData, invalidateQueries }),
  useQuery: (opts: { queryKey: readonly unknown[]; queryFn: () => Promise<unknown> }) => {
    state.queries.push(opts)
    const key = JSON.stringify(opts.queryKey)
    if (!state.data.has(key)) state.data.set(key, ref(undefined))
    return { data: state.data.get(key)!, isPending: ref(false), error: ref(null) }
  },
  // Mutations run inline so a test exercises the real mutationFn and its onSuccess.
  useMutation: (opts: {
    mutationFn: (v: never) => Promise<unknown>
    onSuccess?: (r: unknown) => void
  }) => ({
    isPending: ref(false),
    mutateAsync: async (v: never) => {
      const result = await opts.mutationFn(v)
      opts.onSuccess?.(result)
      return result
    },
  }),
}))

const SERVER: DigestSettings = {
  enabled: false,
  send_time: '07:00:00',
  timezone: 'Europe/Zurich',
  visibility_scope: 'PUBLIC_PROTECTED',
  start_date: '2026-06-01',
  grace_minutes: 120,
  max_email_bytes: 20971520,
  max_videos_per_memo: 4,
  max_memos_per_run: 50,
  mail_title: 'Daily memos',
  from_name: 'memos',
  from_email: '',
  hosting_enabled: true,
  dropbox_folder: '/memos-digest',
  retention_days: 90,
  link_images_full: true,
  delivery_mode: 'attachments' as const,
  updated_at: '2026-08-30T10:00:00Z',
  updated_by: 'fabian',
}

function seedSettings(over: Partial<DigestSettings> = {}) {
  state.data.set(JSON.stringify(['digest', 'settings']), ref({ ...SERVER, ...over }))
}

function queryFor(key: readonly unknown[]) {
  return state.queries.find((q) => JSON.stringify(q.queryKey) === JSON.stringify(key))!
}

describe('useDigest', () => {
  beforeEach(() => {
    state.queries = []
    state.data = new Map()
    setQueryData.mockImplementation((key: readonly unknown[], data: unknown) => {
      // Stand in for the real cache: the response IS the new settings row, and the next writer must
      // build its body from it.
      const k = JSON.stringify(key)
      const existing = state.data.get(k)
      if (existing) existing.value = data
      else state.data.set(k, ref(data))
    })
    PUT.mockResolvedValue({ data: SERVER, error: undefined })
    POST.mockResolvedValue({ data: { id: 1 }, error: undefined })
    PATCH.mockResolvedValue({ data: { id: 1 }, error: undefined })
    DELETE.mockResolvedValue({ error: undefined })
    GET.mockResolvedValue({ data: [], error: undefined })
  })
  afterEach(() => vi.clearAllMocks())

  it('reads the four Phase-1 endpoints, asking the runs table for the last 20', async () => {
    useDigest()
    await queryFor(['digest', 'settings']).queryFn()
    await queryFor(['digest', 'recipients']).queryFn()
    await queryFor(['digest', 'runs']).queryFn()
    await queryFor(['digest', 'hosting']).queryFn()

    expect(GET).toHaveBeenCalledWith('/api/admin/digest/settings')
    expect(GET).toHaveBeenCalledWith('/api/admin/digest/recipients')
    expect(GET).toHaveBeenCalledWith('/api/admin/digest/runs', {
      params: { query: { limit: RUNS_LIMIT } },
    })
    expect(GET).toHaveBeenCalledWith('/api/admin/digest/hosting')
  })

  it('reads the Runs table out of the cursor-paged list, not the envelope', () => {
    state.data.set(
      JSON.stringify(['digest', 'runs']),
      ref({ items: [{ id: 9 }], nextCursor: 'cursor-for-the-page-after', totalCount: null }),
    )
    const digest = useDigest()
    expect(digest.runs.value).toEqual([{ id: 9 }])
  })

  it('shows no runs at all — not an empty list — before the page arrives', () => {
    expect(useDigest().runs.value).toBeUndefined()
  })

  it('surfaces a failed read as a thrown error rather than empty data', async () => {
    GET.mockResolvedValueOnce({ data: undefined, error: { detail: 'nope' } })
    useDigest()
    await expect(queryFor(['digest', 'settings']).queryFn()).rejects.toEqual({ detail: 'nope' })
  })

  it('saves the grouped draft as a whole row, taking enabled from the server', async () => {
    seedSettings({ enabled: true })
    const digest = useDigest()
    const draft = { ...digestDraft(), mail_title: 'Something else' }

    expect(await digest.saveConfig(draft)).toBe(true)
    expect(PUT).toHaveBeenCalledTimes(1)
    expect(PUT).toHaveBeenCalledWith('/api/admin/digest/settings', {
      body: { ...draft, send_time: '07:00', enabled: true },
    })
    expect(setQueryData).toHaveBeenCalledWith(['digest', 'settings'], SERVER)
  })

  it('reports a refused save on the config notice instead of throwing', async () => {
    seedSettings()
    PUT.mockResolvedValueOnce({
      data: undefined,
      error: { detail: 'from_email must be an e-mail address' },
      response: { status: 422 },
    })
    const digest = useDigest()
    expect(await digest.saveConfig(digestDraft())).toBe(false)
    expect(digest.configError.value).toBe('from_email must be an e-mail address')
  })

  it('flips the switch from the SERVER row, carrying no unsaved grouped edit with it', async () => {
    seedSettings({ enabled: false })
    const digest = useDigest()

    expect(await digest.setEnabled(true)).toBe(true)
    expect(PUT).toHaveBeenCalledWith('/api/admin/digest/settings', {
      body: { ...digestDraft(), enabled: true },
    })
    // the draft the admin may be editing is not an argument to this call at all
    expect(PUT.mock.calls[0][1].body.mail_title).toBe(SERVER.mail_title)
  })

  it('refuses a second switch while the first is still on the wire', async () => {
    seedSettings()
    let release: (v: unknown) => void = () => {}
    PUT.mockReturnValueOnce(
      new Promise((resolve) => {
        release = () => resolve({ data: SERVER, error: undefined })
      }),
    )
    const digest = useDigest()

    const first = digest.setEnabled(true)
    const second = await digest.setEnabled(false) // refused, not queued
    release(null)

    expect(second).toBe(false)
    expect(await first).toBe(true)
    expect(PUT).toHaveBeenCalledTimes(1)
  })

  it('refuses a toggle fired while a save of the same row is still on the wire', async () => {
    seedSettings({ enabled: false })
    let release: (v: unknown) => void = () => {}
    PUT.mockReturnValueOnce(
      new Promise((resolve) => {
        release = () => resolve({ data: { ...SERVER, mail_title: 'Changed' }, error: undefined })
      }),
    )
    const digest = useDigest()

    const saving = digest.saveConfig({ ...digestDraft(), mail_title: 'Changed' })
    const toggled = await digest.setEnabled(true) // same row, second writer — refused, not queued
    release(null)

    expect(toggled).toBe(false)
    expect(digest.enabledError.value).toMatch(/still saving/i)
    expect(await saving).toBe(true)
    expect(PUT).toHaveBeenCalledTimes(1)
  })

  it('builds each settings write from the row the previous write installed', async () => {
    seedSettings({ enabled: false, mail_title: 'Daily memos' })
    PUT.mockImplementation(async (_path: string, opts: { body: Record<string, unknown> }) => ({
      data: { ...SERVER, ...opts.body },
      error: undefined,
    }))
    const digest = useDigest()

    expect(await digest.saveConfig({ ...digestDraft(), mail_title: 'Changed' })).toBe(true)
    expect(await digest.setEnabled(true)).toBe(true)

    // the switch's body carries the SAVED title, not the one the page loaded with
    expect(PUT).toHaveBeenCalledTimes(2)
    expect(PUT.mock.calls[1][1].body).toMatchObject({ mail_title: 'Changed', enabled: true })
  })

  it('reports both settings writers as busy while either is in flight', async () => {
    seedSettings()
    let release: (v: unknown) => void = () => {}
    PUT.mockReturnValueOnce(
      new Promise((resolve) => {
        release = () => resolve({ data: SERVER, error: undefined })
      }),
    )
    const digest = useDigest()

    const saving = digest.saveConfig(digestDraft())
    expect(digest.savingConfig.value).toBe(true)
    expect(digest.togglingEnabled.value).toBe(true) // the switch is held too — one row, one gate
    release(null)
    await saving
    expect(digest.savingConfig.value).toBe(false)
    expect(digest.togglingEnabled.value).toBe(false)
  })

  it('adds, toggles and removes a recipient, each on its own request and none of them a settings write', async () => {
    seedSettings()
    const digest = useDigest()

    expect(
      await digest.addRecipient({ email: 'a@example.test', display_name: 'A', language: 'es' }),
    ).toBe(true)
    expect(POST).toHaveBeenCalledWith('/api/admin/digest/recipients', {
      body: { email: 'a@example.test', display_name: 'A', enabled: true, language: 'es' },
    })

    expect(await digest.setRecipientEnabled(7, false)).toBe(true)
    expect(PATCH).toHaveBeenCalledWith('/api/admin/digest/recipients/{recipient_id}', {
      params: { path: { recipient_id: 7 } },
      body: { enabled: false },
    })

    expect(await digest.removeRecipient(7)).toBe(true)
    expect(DELETE).toHaveBeenCalledWith('/api/admin/digest/recipients/{recipient_id}', {
      params: { path: { recipient_id: 7 } },
    })

    expect(PUT).not.toHaveBeenCalled()
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['digest', 'recipients'] })
  })

  it("shows the server's own reason when a recipient change is refused", async () => {
    seedSettings({ enabled: true })
    PATCH.mockResolvedValueOnce({
      data: undefined,
      error: { detail: 'this is the last enabled recipient of an enabled digest' },
      response: { status: 422 },
    })
    const digest = useDigest()

    expect(await digest.setRecipientEnabled(7, false)).toBe(false)
    expect(digest.recipientError.value).toMatch(/last enabled recipient/)
  })

  it('refuses a second recipient change while one is in flight', async () => {
    seedSettings()
    let release: (v: unknown) => void = () => {}
    DELETE.mockReturnValueOnce(
      new Promise((resolve) => {
        release = () => resolve({ error: undefined })
      }),
    )
    const digest = useDigest()

    const first = digest.removeRecipient(1)
    const second = await digest.removeRecipient(2)
    release(null)

    expect(second).toBe(false)
    expect(await first).toBe(true)
    expect(DELETE).toHaveBeenCalledTimes(1)
  })

  it('refuses to write settings at all before the row has loaded', async () => {
    const digest = useDigest() // no seeded settings
    expect(await digest.setEnabled(true)).toBe(false)
    expect(digest.enabledError.value).toMatch(/not loaded/)
    expect(PUT).not.toHaveBeenCalled()
  })
})

// The draft as it comes off the seeded server row — spelled out rather than derived from the
// function under test, so the expected PUT body is an independent statement of the contract.
function digestDraft() {
  return {
    send_time: '07:00',
    timezone: 'Europe/Zurich',
    visibility_scope: 'PUBLIC_PROTECTED' as const,
    start_date: '2026-06-01',
    grace_minutes: 120,
    max_email_bytes: 20971520,
    max_videos_per_memo: 4,
    max_memos_per_run: 50,
    mail_title: 'Daily memos',
    from_name: 'memos',
    from_email: '',
    hosting_enabled: true,
    dropbox_folder: '/memos-digest',
    retention_days: 90,
    link_images_full: true,
    delivery_mode: 'attachments' as const,
  }
}
