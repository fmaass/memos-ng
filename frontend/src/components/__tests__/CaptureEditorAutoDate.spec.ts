import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CaptureEditor from '@/components/CaptureEditor.vue'

/**
 * The composer's Auto memo date and capture order, driven through the REAL upload lane
 * (MEMNG-262): `useResumableUpload` is not stubbed — `fetch` is, so the create/append/inspect
 * round trip the composable actually performs is what these tests exercise.
 */

const { postMemo, getTags, toastAdd } = vi.hoisted(() => ({
  postMemo: vi.fn(),
  getTags: vi.fn(),
  toastAdd: vi.fn(),
}))
// The refusal's SENTENCE is the contract, so it is read off the toast service rather than out of
// a <Toast> container this test never mounts.
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))
vi.mock('@/composables/useApi', () => ({ useApi: () => ({ POST: postMemo, GET: getTags }) }))
vi.mock('@/composables/useSettings', async () => {
  const { ref } = await import('vue')
  return { useSettings: () => ({ settings: ref(undefined), update: { mutate: () => {} } }) }
})

interface CaptureAnswer {
  state?: 'pending' | 'done'
  capture_instant?: string | null
  capture_civil?: string | null
  capture_source?: string | null
}

const json = (body: unknown) => ({
  ok: true,
  status: 200,
  headers: new Headers(),
  json: async () => body,
})

/**
 * A fake upload lane: create → one append → the capture inspection. `script` maps a FILE NAME to
 * the answers its inspection gives, in order — a one-entry list answers every poll the same way,
 * which is how "still pending" and "settled late" are both expressed.
 */
function stubLane(script: Record<string, CaptureAnswer[]>) {
  const nameOf = new Map<string, string>()
  const seen = new Map<string, number>()
  let n = 0
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/uploads' && init?.method === 'POST') {
      const uid = `upload-${++n}`
      nameOf.set(uid, JSON.parse(String(init.body)).filename)
      return json({ upload_uid: uid, chunk_size: 1 << 20, bytes_received: 0 })
    }
    const capture = /^\/api\/uploads\/([^/]+)\/capture$/.exec(url)
    if (capture) {
      const name = nameOf.get(capture[1])!
      const answers = script[name] ?? [{ state: 'done' }]
      const i = Math.min(seen.get(name) ?? 0, answers.length - 1)
      seen.set(name, i + 1)
      const answer = answers[i]
      return json({
        state: answer.state ?? 'done',
        capture_instant: answer.capture_instant ?? null,
        capture_civil: answer.capture_civil ?? null,
        capture_source: answer.capture_source ?? null,
      })
    }
    if (init?.method === 'PATCH') return json({ bytes_received: 3 })
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function mountEditor() {
  return mount(CaptureEditor, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
}

async function attach(wrapper: ReturnType<typeof mountEditor>, ...names: string[]) {
  const input = wrapper.find('[data-test="file-input"]')
  const files = names.map((name) => new File([new Uint8Array(3)], name, { type: 'image/jpeg' }))
  Object.defineProperty(input.element, 'files', { value: files, configurable: true })
  await input.trigger('change')
  await flushPromises()
  await flushPromises()
  await nextTick()
}

const chip = (wrapper: ReturnType<typeof mountEditor>) =>
  wrapper.find('[data-test="auto-date-chip"]').text()

const tileNames = (wrapper: ReturnType<typeof mountEditor>) =>
  wrapper.findAll('[data-test="uploads"] > div').map((d) => d.attributes('title'))

describe('CaptureEditor — the Auto memo date', () => {
  beforeEach(() => {
    postMemo.mockResolvedValue({ data: { uid: 'm1' }, error: undefined })
    getTags.mockResolvedValue({ data: [], error: undefined })
  })
  afterEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('reads "Auto · now" with nothing attached', () => {
    stubLane({})
    expect(chip(mountEditor())).toBe('Auto · now')
  })

  it('dates the memo by the EARLIEST attached photo and posts that instant', async () => {
    stubLane({
      'late.jpg': [{ capture_instant: '2026-01-01T15:00:00Z', capture_source: 'exif' }],
      'early.jpg': [{ capture_instant: '2026-01-01T13:00:00Z', capture_source: 'exif' }],
    })
    const wrapper = mountEditor()
    await attach(wrapper, 'late.jpg', 'early.jpg')

    expect(chip(wrapper)).toContain('Auto · ')
    expect(chip(wrapper)).not.toContain('≈')
    // the tiles are in CAPTURE order, not pick order
    expect(tileNames(wrapper)).toEqual(['early.jpg', 'late.jpg'])

    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    const body = postMemo.mock.calls[0][1].body
    expect(body.created_at).toBe('2026-01-01T13:00:00Z') // the earliest, seconds precision
    expect(body.upload_uids).toEqual(['upload-2', 'upload-1']) // …and in capture order
  })

  it('marks an approximate date with ≈ when the winner is a resolved civil time', async () => {
    stubLane({ 'civil.jpg': [{ capture_civil: '2026-01-01T13:00:00', capture_source: 'exif' }] })
    const wrapper = mountEditor()
    await attach(wrapper, 'civil.jpg')
    expect(chip(wrapper)).toContain('≈')
  })

  it('appends the span hint when the media falls on different calendar days', async () => {
    stubLane({
      'a.jpg': [{ capture_instant: '2026-01-01T12:00:00Z', capture_source: 'exif' }],
      'b.jpg': [{ capture_instant: '2026-01-03T12:00:00Z', capture_source: 'exif' }],
    })
    const wrapper = mountEditor()
    await attach(wrapper, 'a.jpg', 'b.jpg')
    expect(chip(wrapper)).toContain('· spans 3 days')
  })

  it('shows no hint and no date when nothing carries a capture instant', async () => {
    stubLane({ 'plain.jpg': [{}] })
    const wrapper = mountEditor()
    await attach(wrapper, 'plain.jpg')
    expect(chip(wrapper)).toBe('Auto · now')
    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo.mock.calls[0][1].body.created_at).toBeUndefined() // "now" is the server's now
  })

  it('REFUSES to post while a capture decision is still pending, and names the file', async () => {
    stubLane({ 'slow.jpg': [{ state: 'pending' }] })
    const wrapper = mountEditor()
    await attach(wrapper, 'slow.jpg')
    await wrapper.find('[data-test="content"]').setValue('with a photo')

    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()

    expect(postMemo).not.toHaveBeenCalled()
    expect(wrapper.find('[data-test="capture-pending"]').exists()).toBe(true) // the tile is marked
    expect(toastAdd).toHaveBeenCalledTimes(1)
    expect(toastAdd.mock.calls[0][0].detail).toContain('slow.jpg') // one sentence, naming the file
  })

  it('re-dates and re-orders when a decision settles LATE', async () => {
    stubLane({
      'later.jpg': [{ capture_instant: '2026-01-01T14:00:00Z', capture_source: 'exif' }],
      // pending first, then the earlier instant — the second poll is the late completion
      'slow.jpg': [
        { state: 'pending' },
        { capture_instant: '2026-01-01T12:00:00Z', capture_source: 'exif' },
      ],
    })
    const wrapper = mountEditor()
    await attach(wrapper, 'later.jpg', 'slow.jpg')
    expect(tileNames(wrapper)).toEqual(['later.jpg', 'slow.jpg']) // undated sorts last

    await new Promise((resolve) => setTimeout(resolve, 400)) // the poll's first backoff
    await flushPromises()
    await nextTick()

    expect(tileNames(wrapper)).toEqual(['slow.jpg', 'later.jpg']) // it moved to the front
    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo.mock.calls[0][1].body.created_at).toBe('2026-01-01T12:00:00Z')
  })

  it('re-dates when the earliest tile is REMOVED', async () => {
    stubLane({
      'early.jpg': [{ capture_instant: '2026-01-01T13:00:00Z', capture_source: 'exif' }],
      'late.jpg': [{ capture_instant: '2026-01-01T14:00:00Z', capture_source: 'exif' }],
    })
    const wrapper = mountEditor()
    await attach(wrapper, 'early.jpg', 'late.jpg')
    const dated = chip(wrapper)

    await wrapper.findAll('[data-test="upload-remove"]')[0].trigger('click')
    await nextTick()

    expect(tileNames(wrapper)).toEqual(['late.jpg'])
    expect(chip(wrapper)).not.toBe(dated) // the memo now takes the surviving photo's date
    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo.mock.calls[0][1].body.created_at).toBe('2026-01-01T14:00:00Z')
  })

  it('goes Auto → Manual on a picked date, and back to Auto on one click', async () => {
    stubLane({ 'pic.jpg': [{ capture_instant: '2026-01-01T13:00:00Z', capture_source: 'exif' }] })
    const wrapper = mountEditor()
    await attach(wrapper, 'pic.jpg')
    expect(wrapper.find('[data-test="auto-date-chip"]').exists()).toBe(true)

    // the ⋯ menu's "Set custom date…" — invoked through the model the Menu is given, since the
    // popup itself does not open in a DOM without layout
    const menus = wrapper.findAllComponents({ name: 'Menu' })
    const more = menus[menus.length - 1]
    ;(more.props('model') as { label: string; command: () => void }[])[0].command()
    await nextTick()

    const picker = wrapper.findComponent({ name: 'DatePicker' })
    picker.vm.$emit('update:model-value', new Date('2026-06-06T08:00:00Z'))
    await nextTick()
    expect(wrapper.find('[data-test="date-chip"]').exists()).toBe(true) // MANUAL now
    expect(wrapper.find('[data-test="auto-date-chip"]').exists()).toBe(false)

    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo.mock.calls[0][1].body.created_at).toBe('2026-06-06T08:00:00Z')

    // after the post the composer is back on Auto for the next memo
    await nextTick()
    expect(wrapper.find('[data-test="auto-date-chip"]').exists()).toBe(true)
  })

  it('refuses a pending decision in MANUAL mode too — the order depends on it as well', async () => {
    stubLane({ 'slow.jpg': [{ state: 'pending' }] })
    const wrapper = mountEditor()
    await attach(wrapper, 'slow.jpg')
    const menus = wrapper.findAllComponents({ name: 'Menu' })
    ;(
      menus[menus.length - 1].props('model') as { label: string; command: () => void }[]
    )[0].command()
    await nextTick()
    wrapper.findComponent({ name: 'DatePicker' }).vm.$emit('update:model-value', new Date())
    await nextTick()

    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo).not.toHaveBeenCalled()
  })
})
