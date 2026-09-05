import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CaptureEditor from '@/components/CaptureEditor.vue'

import {
  dateVector,
  fileNameOf,
  instantOf,
  orderVector,
  type VectorItem,
} from '@/lib/__tests__/artefact'

/**
 * The composer's Auto memo date and capture order, driven through the REAL upload lane
 * (MEMNG-262): `useResumableUpload` is not stubbed — `fetch` is, so the create/append/inspect
 * round trip the composable actually performs is what these tests exercise.
 *
 * Every scenario is a VECTOR from `fixtures/auto-memo-date.json`, named by its id: the files a
 * test attaches, the answers the fake server gives and the result it expects all come out of that
 * one artefact, so no rule is restated here.
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
}

function answersFor(items: VectorItem[]): Record<string, CaptureAnswer[]> {
  return Object.fromEntries(
    items.map((i) => [
      fileNameOf(i),
      [{ state: 'done', capture_instant: i.instant, capture_civil: i.civil }],
    ]),
  )
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
 * which is how "still pending" and "settled late" are both expressed. `capture` may also be a
 * function, for the request that never answers at all.
 */
function stubLane(
  script: Record<string, CaptureAnswer[] | ((init?: RequestInit) => Promise<unknown>)>,
) {
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
      const scripted = script[name] ?? [{ state: 'done' }]
      if (typeof scripted === 'function') return scripted(init)
      const i = Math.min(seen.get(name) ?? 0, scripted.length - 1)
      seen.set(name, i + 1)
      const answer = scripted[i]
      return json({
        state: answer.state ?? 'done',
        capture_instant: answer.capture_instant ?? null,
        capture_civil: answer.capture_civil ?? null,
        capture_source: answer.capture_instant || answer.capture_civil ? 'exif' : null,
      })
    }
    if (init?.method === 'PATCH') return json({ bytes_received: 3 })
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, uidOf: (name: string) => [...nameOf].find(([, n2]) => n2 === name)![0] }
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

  it('posts the vector "undated-ignored" instant — the earliest, undated media ignored', async () => {
    const vector = dateVector('undated-ignored')
    stubLane(answersFor(vector.items))
    const wrapper = mountEditor()
    await attach(wrapper, ...vector.items.map(fileNameOf))

    expect(chip(wrapper)).not.toContain('≈')
    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo.mock.calls[0][1].body.created_at).toBe(vector.expect.instant)
  })

  it('marks the vector "civil-only" with ≈ — its winner is a resolved civil time', async () => {
    const vector = dateVector('civil-only')
    expect(vector.expect.approximate).toBe(true)
    stubLane(answersFor(vector.items))
    const wrapper = mountEditor()
    await attach(wrapper, ...vector.items.map(fileNameOf))
    expect(chip(wrapper)).toContain('≈')
  })

  it('appends the vector "two-calendar-days" span hint', async () => {
    const vector = dateVector('two-calendar-days')
    stubLane(answersFor(vector.items))
    const wrapper = mountEditor()
    await attach(wrapper, ...vector.items.map(fileNameOf))
    expect(chip(wrapper)).toContain(`· spans ${vector.expect.span} days`)
  })

  it('shows the vector "all-undated" as "Auto · now" and posts no created_at', async () => {
    const vector = dateVector('all-undated')
    expect(vector.expect.instant).toBeNull()
    stubLane(answersFor(vector.items))
    const wrapper = mountEditor()
    await attach(wrapper, ...vector.items.map(fileNameOf))
    expect(chip(wrapper)).toBe('Auto · now')
    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo.mock.calls[0][1].body.created_at).toBeUndefined() // "now" is the server's now
  })

  it('shows and posts the vector "basic" capture order', async () => {
    const vector = orderVector('basic')
    const { uidOf } = stubLane(answersFor(vector.items))
    const wrapper = mountEditor()
    await attach(wrapper, ...vector.items.map(fileNameOf))

    expect(tileNames(wrapper)).toEqual(vector.expect.map((id) => `${id}.jpg`))
    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo.mock.calls[0][1].body.upload_uids).toEqual(
      vector.expect.map((id) => uidOf(`${id}.jpg`)),
    )
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

  it('re-orders and re-dates on the vector "after-late-completion"', async () => {
    const vector = orderVector('after-late-completion')
    const [late, other] = vector.items // the note: `late` settles only on the second poll
    stubLane({
      ...answersFor([other]),
      [fileNameOf(late)]: [
        { state: 'pending' },
        { state: 'done', capture_instant: late.instant, capture_civil: late.civil },
      ],
    })
    const wrapper = mountEditor()
    await attach(wrapper, ...vector.items.map(fileNameOf)) // picked in the vector's own order

    await new Promise((resolve) => setTimeout(resolve, 400)) // the poll's first backoff
    await flushPromises()
    await nextTick()

    expect(tileNames(wrapper)).toEqual(vector.expect.map((id) => `${id}.jpg`))
    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo.mock.calls[0][1].body.created_at).toBe(late.instant) // the late one now wins
  })

  it('re-orders and re-dates on the vector "after-removal"', async () => {
    // The vector is the state AFTER the removal, and its note names what was removed: the
    // earliest item, added first. That item is the artefact's own "single-exact" one.
    const vector = orderVector('after-removal')
    const removed: VectorItem = { id: 'removed', instant: instantOf('single-exact', 'A'), civil: null }
    stubLane(answersFor([removed, ...vector.items]))
    const wrapper = mountEditor()
    await attach(wrapper, ...[removed, ...vector.items].map(fileNameOf))
    expect(tileNames(wrapper)[0]).toBe(fileNameOf(removed))

    await wrapper.findAll('[data-test="upload-remove"]')[0].trigger('click')
    await nextTick()

    expect(tileNames(wrapper)).toEqual(vector.expect.map((id) => `${id}.jpg`))
    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo.mock.calls[0][1].body.created_at).toBe(vector.items[0].instant)
  })

  it('goes Auto → Manual on a picked date, and back to Auto on one click', async () => {
    stubLane({ 'pic.jpg': [{ capture_instant: instantOf('single-exact', 'A') }] })
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
    // Any instant the artefact knows about will do — the subject is the MODE, not the value.
    const manual = instantOf('two-calendar-days', 'A')
    picker.vm.$emit('update:model-value', new Date(manual))
    await nextTick()
    expect(wrapper.find('[data-test="date-chip"]').exists()).toBe(true) // MANUAL now
    expect(wrapper.find('[data-test="auto-date-chip"]').exists()).toBe(false)

    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo.mock.calls[0][1].body.created_at).toBe(manual)

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

describe('CaptureEditor — one post at a time, and it clears only what it posted', () => {
  beforeEach(() => {
    getTags.mockResolvedValue({ data: [], error: undefined })
  })
  afterEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  /** A post that stays in flight until the test lets it land. */
  function deferredPost() {
    let land!: () => void
    postMemo.mockImplementation(
      () =>
        new Promise((resolve) => {
          land = () => resolve({ data: { uid: 'm1' }, error: undefined })
        }),
    )
    return () => land()
  }

  it('sends ONE request for two clicks in the same moment', async () => {
    stubLane({})
    const land = deferredPost()
    const wrapper = mountEditor()
    await wrapper.find('[data-test="content"]').setValue('once')

    const save = wrapper.find('[data-test="save"]')
    // Both clicks before any await resolves — a check on the `saving` ref alone would let the
    // second one through and post the same memo twice.
    save.trigger('click')
    save.trigger('click')
    await flushPromises()

    expect(postMemo).toHaveBeenCalledTimes(1)
    land()
    await flushPromises()
  })

  it('keeps a file attached WHILE the post was in flight, and clears only the posted one', async () => {
    stubLane({
      'posted.jpg': [{ capture_instant: instantOf('single-exact', 'A') }],
      'later.jpg': [{ capture_instant: instantOf('two-calendar-days', 'A') }],
    })
    const land = deferredPost()
    const wrapper = mountEditor()
    await attach(wrapper, 'posted.jpg')
    await wrapper.find('[data-test="content"]').setValue('first memo')

    wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo).toHaveBeenCalledTimes(1)
    expect(postMemo.mock.calls[0][1].body.upload_uids).toHaveLength(1)

    await attach(wrapper, 'later.jpg') // attached while the request is still out
    land()
    await flushPromises()
    await nextTick()

    // the posted tile is released; the one added meanwhile belongs to the NEXT memo and stays
    expect(tileNames(wrapper)).toEqual(['later.jpg'])
    expect(wrapper.emitted('saved')).toBeTruthy()
  })

  it('keeps a DATE picked while the post was in flight', async () => {
    stubLane({})
    const land = deferredPost()
    const wrapper = mountEditor()
    await wrapper.find('[data-test="content"]').setValue('auto-dated memo')

    wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo).toHaveBeenCalledTimes(1)
    expect(postMemo.mock.calls[0][1].body.created_at).toBeUndefined() // posted on Auto

    // the person starts the NEXT memo while the first is still going out, and picks a date for it
    const menus = wrapper.findAllComponents({ name: 'Menu' })
    ;(menus[menus.length - 1].props('model') as { command: () => void }[])[0].command()
    await nextTick()
    const next = instantOf('two-calendar-days', 'A')
    wrapper.findComponent({ name: 'DatePicker' }).vm.$emit('update:model-value', new Date(next))
    await nextTick()

    land()
    await flushPromises()
    await nextTick()

    // the completion resets what it posted — not the date the next memo is already carrying
    expect(wrapper.find('[data-test="date-chip"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="auto-date-chip"]').exists()).toBe(false)
    await wrapper.find('[data-test="content"]').setValue('the next one')
    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo.mock.calls[1][1].body.created_at).toBe(next)
  })
})

describe('CaptureEditor — an inspection that never answers', () => {
  afterEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('aborts each request on its own deadline and settles the tile as "date unknown"', async () => {
    let aborted = 0
    stubLane({
      // A server that accepts the request and never answers. Only the composable's own per-request
      // deadline can end it — without one, the bounded poll would hang on the first attempt.
      'silent.jpg': (init) =>
        new Promise((_resolve, reject) => {
          init!.signal!.addEventListener('abort', () => {
            aborted += 1
            reject(new DOMException('aborted', 'AbortError'))
          })
        }),
    })
    vi.useFakeTimers() // the deadlines are seconds long; what matters is that they FIRE
    const wrapper = mountEditor()
    try {
      await attach(wrapper, 'silent.jpg')
      for (let i = 0; i < 120 && !wrapper.find('[data-test="capture-unknown"]').exists(); i++) {
        await vi.advanceTimersByTimeAsync(1000)
        await nextTick()
      }
    } finally {
      vi.useRealTimers()
    }

    expect(aborted).toBeGreaterThan(0) // the deadline, not the server, ended each request
    expect(wrapper.find('[data-test="capture-unknown"]').exists()).toBe(true)
    // …and the composer is postable again: the decision is settled, just unknown
    expect(wrapper.find('[data-test="capture-pending"]').exists()).toBe(false)
  })
})
