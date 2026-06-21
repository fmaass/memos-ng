import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import MemoCard from '@/components/MemoCard.vue'

// The Delete action routes through PrimeVue's confirm dialog (a service with no ConfirmDialog
// mounted in these unit specs). Mock useConfirm so we can capture the options and invoke accept().
const { requireSpy } = vi.hoisted(() => ({ requireSpy: vi.fn() }))
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: requireSpy }) }))

// The owner actions (⋯) menu is a PrimeVue popup teleported to body — open it, then return the
// freshest matching item (last appended, so stale nodes from prior mounts don't shadow it).
async function openAction(wrapper: ReturnType<typeof mountCard>, testId: string) {
  await wrapper.find('[data-test="memo-edit"]').trigger('click')
  await nextTick()
  const items = document.body.querySelectorAll<HTMLElement>(`[data-test="${testId}"]`)
  return items[items.length - 1]
}

function imageAtt(uid: string) {
  return {
    uid,
    mime: 'image/jpeg',
    status: 'ready' as const,
    blurhash: null,
    width: null,
    height: null,
    duration: null,
    mine: true,
    hasPrint: false,
    hasLegacyPrint: false,
    renditions: [],
    thumbnailUrl: `/api/attachments/${uid}/thumb`,
    previewUrl: `/api/attachments/${uid}/mobile`,
    webUrl: `/api/attachments/${uid}/web`,
    posterUrl: `/api/attachments/${uid}/poster`,
    downloadUrl: `/api/attachments/${uid}/original`,
    printUrl: `/api/attachments/${uid}/print`,
  }
}

function memo(content: string, tags: string[] = [], attachments: ReturnType<typeof imageAtt>[] = []) {
  return {
    uid: 'm1',
    owner: 'alice',
    mine: true,
    content,
    visibility: 'PRIVATE' as const,
    pinned: false,
    created_at: '2026-06-20T00:00:00Z',
    updated_at: '2026-06-20T00:00:00Z',
    archived_at: null,
    tags,
    attachments,
    reactions: [] as { emoji: string; count: number; reacted: boolean }[],
    total_size: 0,
  }
}

function mountCard(props: { memo: ReturnType<typeof memo>; restorable?: boolean }) {
  return mount(MemoCard, { props, global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
}

describe('MemoCard', () => {
  it('renders markdown content and tag chips', () => {
    const wrapper = mountCard({ memo: memo('**bold** memo', ['work', 'idea']) })
    expect(wrapper.find('[data-test="memo-content"]').html()).toContain('<strong>bold</strong>')
    expect(wrapper.findAll('[data-test="tag-chip"]').map((c) => c.text())).toEqual(['#work', '#idea'])
  })

  it('renders a <script> in memo content inert (no live script tag)', () => {
    const wrapper = mountCard({ memo: memo('evil <script>alert(1)</script>') })
    const content = wrapper.find('[data-test="memo-content"]')
    expect(content.element.querySelector('script')).toBeNull()
    expect(content.html()).toContain('&lt;script&gt;')
  })

  it('has no restore action unless restorable', () => {
    const wrapper = mountCard({ memo: memo('x') })
    expect(wrapper.find('[data-test="restore"]').exists()).toBe(false)
  })

  it('emits restore with the uid when restorable', async () => {
    const wrapper = mountCard({ memo: memo('x'), restorable: true })
    await wrapper.find('[data-test="restore"]').trigger('click')
    expect(wrapper.emitted('restore')?.[0]).toEqual(['m1'])
  })

  it('emits task-toggle with flipped content when a task checkbox is clicked', async () => {
    const wrapper = mountCard({ memo: memo('- [ ] todo') })
    const box = wrapper.find('input.task-list-item-checkbox')
    expect(box.exists()).toBe(true)
    await box.trigger('click')
    expect(wrapper.emitted('task-toggle')?.[0]).toEqual([{ uid: 'm1', content: '- [x] todo' }])
  })

  it('opens the lightbox at the clicked image (MediaLightbox modelValue)', async () => {
    const wrapper = mountCard({ memo: memo('pics', [], [imageAtt('img1')]) })
    const lb = wrapper.findComponent({ name: 'MediaLightbox' })
    expect(lb.props('modelValue')).toBe(null) // closed
    await wrapper.find('[data-test="tile-image"]').trigger('click')
    expect(lb.props('modelValue')).toBe(0) // opened at the memo's first attachment
  })

  it('edits content + visibility + date and emits edit on save', async () => {
    const wrapper = mountCard({ memo: memo('original') })
    const editItem = await openAction(wrapper, 'memo-action-edit')
    editItem.click()
    await nextTick()
    expect(wrapper.find('[data-test="memo-editor"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="edit-add-image"]').exists()).toBe(true) // can attach new images
    await wrapper.find('[data-test="edit-content"]').setValue('edited text')
    await wrapper.find('[data-test="edit-save"]').trigger('click')
    const payload = wrapper.emitted('edit')?.[0]?.[0] as {
      uid: string
      content: string
      upload_uids: string[]
    }
    expect(payload.uid).toBe('m1')
    expect(payload.content).toBe('edited text')
    expect(payload.upload_uids).toEqual([]) // no new uploads staged this edit
    wrapper.unmount()
  })

  it('edit mode shows a remove button for each existing image', async () => {
    const wrapper = mountCard({ memo: memo('pics', [], [imageAtt('img1')]) })
    const editItem = await openAction(wrapper, 'memo-action-edit')
    editItem.click()
    await nextTick()
    expect(wrapper.find('[data-test="edit-remove-attachment"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('emits delete-memo when the Delete action is confirmed', async () => {
    requireSpy.mockClear()
    const wrapper = mountCard({ memo: memo('to delete') })
    const delItem = await openAction(wrapper, 'memo-action-delete')
    delItem.click()
    expect(requireSpy).toHaveBeenCalledTimes(1)
    // No emit until the user actually confirms.
    expect(wrapper.emitted('delete-memo')).toBeUndefined()
    const opts = requireSpy.mock.calls[0][0] as { accept: () => void }
    opts.accept()
    expect(wrapper.emitted('delete-memo')?.[0]).toEqual(['m1'])
    wrapper.unmount()
  })

  it('hides the owner actions on memos the user does not own', () => {
    const wrapper = mountCard({ memo: { ...memo('x'), mine: false } })
    expect(wrapper.find('[data-test="memo-edit"]').exists()).toBe(false)
  })

  it('isolates the card and elevates the actions button above content', () => {
    const wrapper = mountCard({ memo: memo('x') })
    // `isolate` makes the article its own stacking context so z-index resolves within the card;
    // z-20 lifts the ⋯ button above the media grid / markdown (else it is occluded / "you don't see").
    expect(wrapper.find('[data-test="memo-card"]').classes()).toContain('isolate')
    expect(wrapper.find('[data-test="memo-edit"]').classes()).toContain('z-20')
  })

  it('adds a reaction from the picker (PrimeVue Popover, teleported)', async () => {
    const wrapper = mountCard({ memo: memo('hi') })
    await wrapper.find('[data-test="react-add"]').trigger('click')
    await nextTick()
    // Popover content teleports to body — query there, not the wrapper.
    const choice = document.body.querySelector('[data-test="react-choice-👍"]') as HTMLElement
    expect(choice).toBeTruthy()
    choice.click()
    expect(wrapper.emitted('toggle-reaction')?.[0]).toEqual([
      { uid: 'm1', emoji: '👍', add: true },
    ])
  })

  it('toggles off an existing reaction when its chip is clicked', async () => {
    const m = { ...memo('hi'), reactions: [{ emoji: '❤️', count: 2, reacted: true }] }
    const wrapper = mountCard({ memo: m })
    await wrapper.find('[data-test="reaction-chip"]').trigger('click')
    expect(wrapper.emitted('toggle-reaction')?.[0]).toEqual([
      { uid: 'm1', emoji: '❤️', add: false },
    ])
  })
})
