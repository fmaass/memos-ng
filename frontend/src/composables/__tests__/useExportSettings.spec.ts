import { type Ref, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { EXPORT_SETTINGS_PATH, useExportSettings } from '@/composables/useExportSettings'
import type { ExportSettings, MailProfileDraft, PrintProfileDraft } from '@/lib/exportSettings'
import { mailDraftFromSettings, printDraftFromSettings } from '@/lib/exportSettings'

const { GET, PUT, setQueryData, invalidateQueries, state } = vi.hoisted(() => ({
  GET: vi.fn(),
  PUT: vi.fn(),
  setQueryData: vi.fn(),
  invalidateQueries: vi.fn(),
  state: {
    queries: [] as { queryKey: readonly unknown[]; queryFn: () => Promise<unknown> }[],
    data: new Map<string, Ref<unknown>>(),
  },
}))

vi.mock('@/composables/useApi', () => ({ useApi: () => ({ GET, PUT }) }))
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

const SERVER: ExportSettings = {
  print_long_edge: 4096,
  print_jpeg_quality: 92,
  print_metadata_policy: 'strip',
  print_grayscale_single_channel: true,
  mail_long_edge: 1600,
  mail_jpeg_quality: 80,
  mail_subsampling: '4:2:0',
  mail_progressive: false,
  print_profile_key: 'p4096-q92-strip-mono',
  updated_at: '2026-08-30T10:00:00Z',
  updated_by: 'fabian',
}

const printDraft = (over: Partial<PrintProfileDraft> = {}): PrintProfileDraft => ({
  ...printDraftFromSettings(SERVER),
  ...over,
})
const mailDraft = (over: Partial<MailProfileDraft> = {}): MailProfileDraft => ({
  ...mailDraftFromSettings(SERVER),
  ...over,
})

function seedSettings(over: Partial<ExportSettings> = {}) {
  state.data.set(JSON.stringify(['export', 'settings']), ref({ ...SERVER, ...over }))
}

function queryFor(key: readonly unknown[]) {
  return state.queries.find((q) => JSON.stringify(q.queryKey) === JSON.stringify(key))!
}

describe('useExportSettings', () => {
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
    GET.mockResolvedValue({ data: SERVER, error: undefined })
    PUT.mockResolvedValue({ data: SERVER, error: undefined })
  })
  afterEach(() => vi.clearAllMocks())

  it('reads the settings row from the one endpoint this composable owns', async () => {
    useExportSettings()
    await queryFor(['export', 'settings']).queryFn()
    expect(GET).toHaveBeenCalledWith(EXPORT_SETTINGS_PATH)
    expect(EXPORT_SETTINGS_PATH).toBe('/api/admin/export/settings')
  })

  it('surfaces a failed read as a thrown error rather than empty data', async () => {
    GET.mockResolvedValueOnce({ data: undefined, error: { detail: 'nope' } })
    useExportSettings()
    await expect(queryFor(['export', 'settings']).queryFn()).rejects.toEqual({ detail: 'nope' })
  })

  it('saves the print draft as a whole row, taking the mail fields from the server', async () => {
    seedSettings({ mail_long_edge: 2048, mail_progressive: true })
    const store = useExportSettings()

    expect(await store.savePrint(printDraft({ print_long_edge: 5000 }))).toBe(true)
    expect(PUT).toHaveBeenCalledTimes(1)
    expect(PUT).toHaveBeenCalledWith(EXPORT_SETTINGS_PATH, {
      body: {
        print_long_edge: 5000,
        print_jpeg_quality: 92,
        print_metadata_policy: 'strip',
        print_grayscale_single_channel: true,
        mail_long_edge: 2048,
        mail_jpeg_quality: 80,
        mail_subsampling: '4:2:0',
        mail_progressive: true,
      },
    })
    expect(setQueryData).toHaveBeenCalledWith(['export', 'settings'], SERVER)
  })

  it('saves the mail draft as a whole row, taking the print fields from the server', async () => {
    seedSettings({ print_long_edge: 0, print_jpeg_quality: 95 })
    const store = useExportSettings()

    expect(await store.saveMail(mailDraft({ mail_subsampling: '4:4:4' }))).toBe(true)
    expect(PUT.mock.calls[0][1].body).toMatchObject({
      print_long_edge: 0,
      print_jpeg_quality: 95,
      mail_subsampling: '4:4:4',
    })
  })

  it('reports a refused save on its own bar instead of throwing', async () => {
    seedSettings()
    PUT.mockResolvedValueOnce({
      data: undefined,
      error: { detail: 'print_jpeg_quality must be between 90 and 95' },
      response: { status: 422 },
    })
    const store = useExportSettings()
    expect(await store.savePrint(printDraft())).toBe(false)
    expect(store.printError.value).toBe('print_jpeg_quality must be between 90 and 95')
    expect(store.mailError.value).toBeNull() // the other bar says nothing
  })

  it('says so honestly when the endpoint is not there at all', async () => {
    seedSettings()
    PUT.mockResolvedValueOnce({
      data: undefined,
      error: { detail: 'Not Found' },
      response: { status: 404 },
    })
    const store = useExportSettings()
    expect(await store.saveMail(mailDraft())).toBe(false)
    expect(store.mailError.value).toBe('Not Found')
  })

  it('refuses a second profile save while the first is still on the wire', async () => {
    seedSettings()
    let release: (v: unknown) => void = () => {}
    PUT.mockReturnValueOnce(
      new Promise((resolve) => {
        release = () => resolve({ data: SERVER, error: undefined })
      }),
    )
    const store = useExportSettings()

    const first = store.savePrint(printDraft({ print_jpeg_quality: 95 }))
    const second = await store.saveMail(mailDraft({ mail_jpeg_quality: 77 })) // refused, not queued
    release(null)

    expect(second).toBe(false)
    expect(store.mailError.value).toMatch(/still saving/i)
    expect(await first).toBe(true)
    expect(PUT).toHaveBeenCalledTimes(1) // one request on the wire, not two
  })

  it('refuses a double-click on the same bar just as firmly', async () => {
    seedSettings()
    let release: (v: unknown) => void = () => {}
    PUT.mockReturnValueOnce(
      new Promise((resolve) => {
        release = () => resolve({ data: SERVER, error: undefined })
      }),
    )
    const store = useExportSettings()

    const first = store.savePrint(printDraft({ print_jpeg_quality: 95 }))
    expect(await store.savePrint(printDraft({ print_jpeg_quality: 90 }))).toBe(false)
    release(null)
    expect(await first).toBe(true)
    expect(PUT).toHaveBeenCalledTimes(1)
  })

  it('builds each write from the row the previous write installed', async () => {
    seedSettings()
    PUT.mockImplementation(async (_path: string, opts: { body: Record<string, unknown> }) => ({
      data: { ...SERVER, ...opts.body },
      error: undefined,
    }))
    const store = useExportSettings()

    expect(await store.savePrint(printDraft({ print_long_edge: 5000 }))).toBe(true)
    expect(await store.saveMail(mailDraft({ mail_progressive: true }))).toBe(true)

    // the mail bar's body carries the SAVED long edge, not the one the page loaded with
    expect(PUT).toHaveBeenCalledTimes(2)
    expect(PUT.mock.calls[1][1].body).toMatchObject({
      print_long_edge: 5000,
      mail_progressive: true,
    })
  })

  it('reports the saving profile, and only that one, while a write is in flight', async () => {
    seedSettings()
    let release: (v: unknown) => void = () => {}
    PUT.mockReturnValueOnce(
      new Promise((resolve) => {
        release = () => resolve({ data: SERVER, error: undefined })
      }),
    )
    const store = useExportSettings()

    const saving = store.savePrint(printDraft())
    expect(store.savingPrint.value).toBe(true)
    expect(store.savingMail.value).toBe(false)
    expect(store.writerBusy.value).toBe(true)
    release(null)
    await saving
    expect(store.savingPrint.value).toBe(false)
    expect(store.writerBusy.value).toBe(false)
  })

  it('refuses to build a body before the row has been read', async () => {
    const store = useExportSettings() // nothing seeded: the query has not answered
    expect(await store.savePrint(printDraft())).toBe(false)
    expect(store.printError.value).toMatch(/not loaded yet/)
    expect(PUT).not.toHaveBeenCalled()
  })

  it('confirms a save on the shared notice, and clears it on demand', async () => {
    seedSettings()
    const store = useExportSettings()
    await store.savePrint(printDraft())
    expect(store.notice.value).toMatch(/print profile is saved/i)
    await store.saveMail(mailDraft())
    expect(store.notice.value).toMatch(/mail profile is saved/i)
    store.clearNotice()
    expect(store.notice.value).toBeNull()
  })
  describe('a row that already holds an out-of-band value', () => {
    it('blocks a Mail save, names the Print profile, and puts nothing on the wire', async () => {
      seedSettings({ print_long_edge: 3000 }) // e.g. written before the bands existed
      const store = useExportSettings()

      expect(await store.saveMail(mailDraft({ mail_jpeg_quality: 77 }))).toBe(false)
      expect(store.mailError.value).toBe(
        'The Print profile holds a value outside its bands — open it and fix it first.',
      )
      expect(PUT).not.toHaveBeenCalled() // never sent, so no 422 about a field nobody touched
      expect(store.notice.value).toBeNull()
    })

    it('blocks a Print save on a bad stored mail value, naming Mail', async () => {
      seedSettings({ mail_jpeg_quality: 40 })
      const store = useExportSettings()
      expect(await store.savePrint(printDraft())).toBe(false)
      expect(store.printError.value).toMatch(/Mail profile holds a value outside its bands/)
      expect(PUT).not.toHaveBeenCalled()
    })

    it('releases the writer gate, so the fix can be saved straight after', async () => {
      seedSettings({ print_long_edge: 3000 })
      const store = useExportSettings()
      expect(await store.saveMail(mailDraft())).toBe(false)
      expect(store.writerBusy.value).toBe(false)

      // the admin opens Print and saves a legal value: that save carries the mail half and goes
      seedSettings()
      const fixed = useExportSettings()
      expect(await fixed.savePrint(printDraft({ print_long_edge: 4096 }))).toBe(true)
      expect(PUT).toHaveBeenCalledTimes(1)
    })
  })
})
