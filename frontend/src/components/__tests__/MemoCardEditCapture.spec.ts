import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import MemoCard from '@/components/MemoCard.vue'

import { fileNameOf, instantOf, orderVector } from '@/lib/__tests__/artefact'

/**
 * The EDIT half of the settle-before-save rule (MEMNG-262): an edit opens in MANUAL on the memo's
 * own date and never offers Auto, but the files it ADDS still have to settle before Save — they
 * are appended in capture order among themselves, and that order is what the server saves.
 */

vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: vi.fn() }) }))
vi.mock('@/composables/useMediaLightbox', () => ({
  useMediaLightbox: () => ({ open: vi.fn(), destroy: vi.fn(), refreshPrintMaster: vi.fn() }),
}))
vi.mock('@/composables/usePrintMaster', () => ({
  printMasterUploads: () => new Map(),
  fetchPrintMasters: vi.fn().mockResolvedValue([]),
}))

const json = (body: unknown) => ({
  ok: true,
  status: 200,
  headers: new Headers(),
  json: async () => body,
})

/** create → one append → the capture inspection, with the answers under the test's control. */
function stubLane(
  script: Record<string, { state?: 'pending' | 'done'; capture_instant?: string | null }>,
) {
  const nameOf = new Map<string, string>()
  let n = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/uploads' && init?.method === 'POST') {
        const uid = `upload-${++n}`
        nameOf.set(uid, JSON.parse(String(init.body)).filename)
        return json({ upload_uid: uid, chunk_size: 1 << 20, bytes_received: 0 })
      }
      const capture = /^\/api\/uploads\/([^/]+)\/capture$/.exec(url)
      if (capture) {
        const answer = script[nameOf.get(capture[1])!] ?? {}
        return json({
          state: answer.state ?? 'done',
          capture_instant: answer.capture_instant ?? null,
          capture_civil: null,
          capture_source: answer.capture_instant ? 'exif' : null,
        })
      }
      if (init?.method === 'PATCH') return json({ bytes_received: 3 })
      return json({})
    }),
  )
}

const memo = {
  uid: 'm1',
  owner: 'me',
  mine: true,
  content: 'a memo',
  visibility: 'PRIVATE' as const,
  pinned: false,
  created_at: '2026-06-20T00:00:00Z',
  updated_at: '2026-06-20T00:00:00Z',
  archived_at: null,
  tags: [] as string[],
  attachments: [],
  reactions: [] as { emoji: string; count: number; reacted: boolean }[],
  total_size: 0,
}

function mountCard(props: { editPending?: boolean } = {}) {
  return mount(MemoCard, {
    props: { memo, ...props },
    global: { plugins: [PrimeVue, ConfirmationService, ToastService] },
  })
}

async function openEditor(wrapper: ReturnType<typeof mountCard>) {
  await wrapper.find('[data-test="memo-edit"]').trigger('click')
  await nextTick()
  const items = document.body.querySelectorAll<HTMLElement>('[data-test="memo-action-edit"]')
  items[items.length - 1].click()
  await nextTick()
}

async function addFiles(wrapper: ReturnType<typeof mountCard>, ...names: string[]) {
  const input = wrapper.find('[data-test="edit-file-input"]')
  const files = names.map((name) => new File([new Uint8Array(3)], name, { type: 'image/jpeg' }))
  Object.defineProperty(input.element, 'files', { value: files, configurable: true })
  await input.trigger('change')
  await flushPromises()
  await flushPromises()
  await nextTick()
}

describe('MemoCard — the edit path waits for the capture decisions', () => {
  afterEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
  })

  it('refuses Save while an added file is still being read, and says so', async () => {
    stubLane({ 'slow.jpg': { state: 'pending' } })
    const wrapper = mountCard()
    await openEditor(wrapper)
    await addFiles(wrapper, 'slow.jpg')

    const save = wrapper.find('[data-test="edit-save"]')
    expect(wrapper.find('[data-test="edit-capture-pending"]').text()).toContain('slow.jpg')
    await save.trigger('click')
    expect(wrapper.emitted('edit')).toBeFalsy()
  })

  it('shows AND sends the vector "basic" capture order — the display is the saved order', async () => {
    const vector = orderVector('basic')
    stubLane(
      Object.fromEntries(
        vector.items.map((i) => [fileNameOf(i), { capture_instant: i.instant }]),
      ),
    )
    const wrapper = mountCard()
    await openEditor(wrapper)
    await addFiles(wrapper, ...vector.items.map(fileNameOf))

    expect(wrapper.find('[data-test="edit-capture-pending"]').exists()).toBe(false)
    const shown = wrapper
      .findAll('[data-test="edit-attachments"] > div')
      .map((d) => d.find('img').attributes('alt'))
    expect(shown).toEqual(vector.expect.map((id) => `${id}.jpg`)) // what you see…

    await wrapper.find('[data-test="edit-save"]').trigger('click')
    const payload = wrapper.emitted('edit')![0][0] as { upload_uids: string[]; created_at: string }
    const uidOfName = (name: string) => `upload-${vector.items.findIndex((i) => fileNameOf(i) === name) + 1}`
    expect(payload.upload_uids).toEqual(vector.expect.map((id) => uidOfName(`${id}.jpg`))) // …is what is saved
    // MANUAL, always: the edit keeps the memo's own date, in whole seconds like the composer
    expect(payload.created_at).toBe('2026-06-20T00:00:00Z')
  })

  it('emits ONE edit for two clicks in the same moment', async () => {
    stubLane({ 'pic.jpg': { capture_instant: instantOf('single-exact', 'A') } })
    const wrapper = mountCard()
    await openEditor(wrapper)
    await addFiles(wrapper, 'pic.jpg')

    const save = wrapper.find('[data-test="edit-save"]')
    save.trigger('click')
    save.trigger('click')
    await flushPromises()
    expect(wrapper.emitted('edit')).toHaveLength(1)
  })

  it('marks a file whose date could not be read, and still lets the edit be saved', async () => {
    stubLane({ 'silent.jpg': { state: 'pending' } })
    const wrapper = mountCard()
    await openEditor(wrapper)
    vi.useFakeTimers()
    try {
      await addFiles(wrapper, 'silent.jpg')
      for (let i = 0; i < 120 && !wrapper.find('[data-test="edit-capture-unknown"]').exists(); i++) {
        await vi.advanceTimersByTimeAsync(1000)
        await nextTick()
      }
    } finally {
      vi.useRealTimers()
    }
    expect(wrapper.find('[data-test="edit-capture-unknown"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="edit-capture-pending"]').exists()).toBe(false)
    await wrapper.find('[data-test="edit-save"]').trigger('click')
    expect(wrapper.emitted('edit')).toHaveLength(1) // settled, just undated
  })
})


describe('MemoCard — one edit at a time, until the parent says it is over', () => {
  afterEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
  })

  it('will not send a second PATCH when the editor is REOPENED while the first is in flight', async () => {
    stubLane({ 'pic.jpg': { capture_instant: instantOf('single-exact', 'A') } })
    const wrapper = mountCard()
    await openEditor(wrapper)
    await addFiles(wrapper, 'pic.jpg')
    await wrapper.find('[data-test="edit-save"]').trigger('click')
    expect(wrapper.emitted('edit')).toHaveLength(1)

    // the parent's PATCH is now out; the card is told so, and the person reopens the panel
    await wrapper.setProps({ editPending: true })
    await openEditor(wrapper)
    expect(wrapper.find('[data-test="edit-in-flight"]').exists()).toBe(true) // it SAYS why
    expect(wrapper.find('[data-test="edit-save"]').attributes('disabled')).toBeDefined()

    await wrapper.find('[data-test="edit-save"]').trigger('click')
    expect(wrapper.emitted('edit')).toHaveLength(1) // still one — the claim outlives the reopen
  })

  it('accepts a new edit once the parent reports the request finished', async () => {
    stubLane({})
    const wrapper = mountCard()
    await openEditor(wrapper)
    await wrapper.find('[data-test="edit-save"]').trigger('click')
    expect(wrapper.emitted('edit')).toHaveLength(1)

    await wrapper.setProps({ editPending: true })
    await wrapper.setProps({ editPending: false }) // landed, or failed — either way it is over
    await openEditor(wrapper)
    expect(wrapper.find('[data-test="edit-in-flight"]').exists()).toBe(false)
    await wrapper.find('[data-test="edit-save"]').trigger('click')
    expect(wrapper.emitted('edit')).toHaveLength(2)
  })
})
