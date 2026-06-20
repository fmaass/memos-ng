import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CaptureEditor from '@/components/CaptureEditor.vue'

const { postMemo, getTags } = vi.hoisted(() => ({ postMemo: vi.fn(), getTags: vi.fn() }))
vi.mock('@/composables/useApi', () => ({
  useApi: () => ({ POST: postMemo, GET: getTags }),
}))

function mountEditor() {
  return mount(CaptureEditor, { global: { plugins: [PrimeVue] } })
}

describe('CaptureEditor', () => {
  beforeEach(() => {
    postMemo.mockResolvedValue({ data: { uid: 'm1' }, error: undefined })
    getTags.mockResolvedValue({ data: [{ name: 'work', count: 2 }], error: undefined })
  })
  afterEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
  })

  it('posts the memo content + visibility on save, emits saved, and clears the input', async () => {
    const wrapper = mountEditor()
    const textarea = wrapper.find('[data-test="content"]')
    await textarea.setValue('Hello #work')

    await wrapper.find('[data-test="save"]').trigger('click')
    await flushPromises()

    expect(postMemo).toHaveBeenCalledWith('/api/memos', {
      body: { content: 'Hello #work', visibility: 'PRIVATE', pinned: false, attachment_uids: [] },
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

  it('applies a toolbar action to the content', async () => {
    const wrapper = mountEditor()
    await wrapper.find('[data-test="content"]').setValue('hi')
    await wrapper.find('[data-test="md-bold"]').trigger('click')
    expect((wrapper.find('[data-test="content"]').element as HTMLTextAreaElement).value).toContain(
      '**',
    )
  })

  it('enqueues dropped/selected files for upload', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    const wrapper = mountEditor()
    const input = wrapper.find('[data-test="file-input"]')
    const file = new File([new Uint8Array(3)], 'pic.png', { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await nextTick()
    expect(wrapper.find('[data-test="uploads"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('pic.png')
  })
})
