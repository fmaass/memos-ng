import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CaptureEditor from '@/components/CaptureEditor.vue'

const { postMemo, getTags } = vi.hoisted(() => ({ postMemo: vi.fn(), getTags: vi.fn() }))
vi.mock('@/composables/useApi', () => ({
  useApi: () => ({ POST: postMemo, GET: getTags }),
}))
vi.mock('@/composables/useSettings', async () => {
  const { ref } = await import('vue')
  return { useSettings: () => ({ settings: ref(undefined), update: { mutate: () => {} } }) }
})

function mountEditor() {
  return mount(CaptureEditor, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
}

describe('CaptureEditor', () => {
  beforeEach(() => {
    postMemo.mockResolvedValue({ data: { uid: 'm1' }, error: undefined })
    getTags.mockResolvedValue({ data: [{ name: 'work', count: 2 }], error: undefined })
  })
  afterEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  // Queue a real image so the composable mints a preview object URL; fetch is offline, so the
  // upload errors out but the tile (and its object URL + File ref) stay in the queue.
  async function queueImage(wrapper: ReturnType<typeof mountEditor>, name = 'pic.png') {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    const input = wrapper.find('[data-test="file-input"]')
    const file = new File([new Uint8Array(3)], name, { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await nextTick()
  }

  it('posts the memo content + visibility on save, emits saved, and clears the input', async () => {
    const wrapper = mountEditor()
    const textarea = wrapper.find('[data-test="content"]')
    await textarea.setValue('Hello #work')

    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()

    expect(postMemo).toHaveBeenCalledWith('/api/memos', {
      body: { content: 'Hello #work', visibility: 'PRIVATE', pinned: false, upload_uids: [] },
    })
    expect(wrapper.emitted('saved')).toBeTruthy()
    expect((textarea.element as HTMLTextAreaElement).value).toBe('')
  })

  it('does not post an empty memo', async () => {
    const wrapper = mountEditor()
    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(postMemo).not.toHaveBeenCalled()
  })

  it('applies a markdown action via the ⌘B shortcut', async () => {
    const wrapper = mountEditor()
    const ta = wrapper.find('[data-test="content"]')
    await ta.setValue('hi')
    await ta.trigger('keydown', { key: 'b', metaKey: true })
    expect((ta.element as HTMLTextAreaElement).value).toContain('**')
  })

  it('posts on ⌘+Enter', async () => {
    const wrapper = mountEditor()
    const ta = wrapper.find('[data-test="content"]')
    await ta.setValue('quick note')
    await ta.trigger('keydown', { key: 'Enter', metaKey: true })
    await flushPromises()
    expect(postMemo).toHaveBeenCalled()
  })

  it('enqueues dropped/selected files for upload', async () => {
    const wrapper = mountEditor()
    await queueImage(wrapper)
    expect(wrapper.find('[data-test="uploads"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="uploads"] [title="pic.png"]').exists()).toBe(true)
  })

  // MEMNG-46 wiring pin: save() must release the queue through the composable, not `uploads.length = 0`
  // (which empties the array without ever revoking the preview URLs).
  it('releases the upload queue on save — every preview object URL is revoked', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock/pic.png')
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const wrapper = mountEditor()
    await queueImage(wrapper)
    await wrapper.find('[data-test="content"]').setValue('with a photo')

    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="uploads"]').exists()).toBe(false) // queue emptied
    expect(revoke).toHaveBeenCalledWith('blob:mock/pic.png') // …and actually released
  })
})
