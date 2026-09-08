import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import MemoCard from '@/components/MemoCard.vue'

// The Delete action routes through PrimeVue's confirm dialog (a service with no ConfirmDialog
// mounted in these unit specs). Mock useConfirm so we can capture the options and invoke accept().
const { requireSpy } = vi.hoisted(() => ({ requireSpy: vi.fn() }))
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: requireSpy }) }))

// The viewer is PhotoSwipe (imperative, needs a real DOM) — mock the composable so we can assert the
// open() wiring without booting PhotoSwipe in jsdom.
const { lightboxOpen } = vi.hoisted(() => ({ lightboxOpen: vi.fn() }))
vi.mock('@/composables/useMediaLightbox', () => ({
  useMediaLightbox: () => ({
    open: lightboxOpen,
    destroy: vi.fn(),
    refreshPrintMaster: vi.fn(),
  }),
}))

// The print-master mutations live in the view above this card; the card only reads the shared
// upload queue (for its chips) and emits. Unit-tested in composables/__tests__/usePrintMaster.spec.
const { printUploads } = vi.hoisted(() => ({ printUploads: new Map() }))
vi.mock('@/composables/usePrintMaster', () => ({
  printMasterUploads: () => printUploads,
  fetchPrintMasters: vi.fn().mockResolvedValue([]),
}))

// The owner actions (⋯) menu is a PrimeVue popup teleported to body — open it, then return the
// freshest matching item (last appended, so stale nodes from prior mounts don't shadow it).
async function openAction(wrapper: ReturnType<typeof mountCard>, testId: string) {
  await wrapper.find('[data-test="memo-edit"]').trigger('click')
  await nextTick()
  const items = document.body.querySelectorAll<HTMLElement>(`[data-test="${testId}"]`)
  return items[items.length - 1]
}

function imageAtt(uid: string, dims: { width: number | null; height: number | null } = { width: null, height: null }) {
  return {
    uid,
    mime: 'image/jpeg',
    status: 'ready' as const,
    blurhash: null,
      sha256: null,
    ...dims,
    duration: null,
    mine: true,
    hasPrint: false,
    hasLegacyPrint: false,
    position: null,
    // widened, not narrowed to null: a spec that gives a photo a real capture read spreads it
    // over this shape, which `null` alone would type as accepting nothing else
    capture_instant: null as string | null,
    capture_civil: null as string | null,
    capture_source: null as string | null,
    renditions: [] as {
      variant_key: string
      label: string | null
      mime: string
      size: number
      downloadUrl: string
      rev: string | null
    }[],
    thumbnailUrl: `/api/attachments/${uid}/thumb`,
    previewUrl: `/api/attachments/${uid}/mobile`,
    webUrl: `/api/attachments/${uid}/web`,
    posterUrl: `/api/attachments/${uid}/poster`,
    downloadUrl: `/api/attachments/${uid}/original`,
    printUrl: `/api/attachments/${uid}/print`,
  }
}

// The same photo, carrying a print master (what the API ships on every feed page).
function withMaster(att: ReturnType<typeof imageAtt>, variantKey = 'print') {
  return {
    ...att,
    hasPrint: true,
    renditions: [
      {
        variant_key: variantKey,
        label: null,
        mime: 'image/tiff',
        size: 8_598_323,
        downloadUrl: `/api/attachments/${att.uid}/rendition/${variantKey}`,
        rev: 'a1b2c3d4',
      },
    ],
  }
}

function memo(
  content: string,
  tags: string[] = [],
  attachments: ReturnType<typeof imageAtt>[] = [],
  createdAt = '2026-06-20T00:00:00Z',
) {
  return {
    uid: 'm1',
    owner: 'alice',
    mine: true,
    content,
    visibility: 'PRIVATE' as const,
    pinned: false,
    created_at: createdAt,
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

// happy-dom measures every element as 0 wide; justified rows need a real container width, so drive
// it through a ResizeObserver stub (the same path a browser takes).
function stubResizeObserver(width: number) {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(private cb: ResizeObserverCallback) {}
      observe() {
        this.cb([{ contentRect: { width } } as ResizeObserverEntry], this as ResizeObserver)
      }
      disconnect() {}
      unobserve() {}
    },
  )
}

describe('MemoCard', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    printUploads.clear() // module-level queue — a leftover entry would chip the next test's tiles
  })

  describe('the memo date', () => {
    // Midday UTC: every zone within ±11 hours agrees on the calendar day, so the assertion below
    // is about the card and not about where this suite happens to run.
    const NOON = '2026-06-20T12:00:00Z'

    it('shows the memo date', () => {
      const wrapper = mountCard({ memo: memo('dated', [], [], NOON) })
      const shown = wrapper.find('[data-test="memo-date"]')
      expect(shown.attributes('datetime')).toBe(NOON)
      expect(shown.text()).toContain('Jun 20, 2026')
    })
  })

  it('renders markdown content and tag chips', () => {
    const wrapper = mountCard({ memo: memo('**bold** memo', ['work', 'idea']) })
    expect(wrapper.find('[data-test="memo-content"]').html()).toContain('<strong>bold</strong>')
    expect(wrapper.findAll('[data-test="tag-chip"]').map((c) => c.text())).toEqual(['#work', '#idea'])
  })

  it('shows a tag only as a chip, not as a title in the body', () => {
    const wrapper = mountCard({ memo: memo('#curated', ['curated']) })
    expect(wrapper.find('[data-test="memo-content"]').text()).not.toContain('#curated')
    expect(wrapper.findAll('[data-test="tag-chip"]').map((c) => c.text())).toEqual(['#curated'])
  })

  it('states the print badge as a COUNT of the photos that have a master', () => {
    // The old badge was a flag: two photos with one master looked exactly like two with two.
    const withPrint = mountCard({
      memo: memo('trip', [], [withMaster(imageAtt('p1')), imageAtt('p2')]),
    })
    expect(withPrint.find('[data-test="print-badge"]').text()).toBe('🖨 1 of 2')

    const both = mountCard({
      memo: memo('trip', [], [withMaster(imageAtt('p1')), withMaster(imageAtt('p2'))]),
    })
    expect(both.find('[data-test="print-badge"]').text()).toBe('🖨 2 of 2')

    const without = mountCard({ memo: memo('trip', [], [imageAtt('p2')]) })
    expect(without.find('[data-test="print-badge"]').exists()).toBe(false)
  })

  it('chips each photo with its OWN print state, including a live upload', () => {
    printUploads.set('p2', { status: 'uploading', progress: 42 })
    const wrapper = mountCard({
      memo: memo('trip', [], [withMaster(imageAtt('p1')), imageAtt('p2')]),
    })
    const chips = wrapper.findAll('[data-test="print-chip"]')
    expect(chips.map((c) => c.text())).toEqual(['PRINT', 'UPLOADING 42%'])
    expect(chips.map((c) => c.attributes('data-chip-kind'))).toEqual(['present', 'uploading'])
  })

  it('offers RETRY on a failed print-master upload and bubbles it', async () => {
    printUploads.set('p1', { status: 'error', progress: 12 })
    const wrapper = mountCard({ memo: memo('trip', [], [imageAtt('p1'), imageAtt('p2')]) })
    const chip = wrapper.find('[data-test="print-chip"]')
    expect(chip.text()).toBe('PRINT FAILED · RETRY')

    await chip.trigger('click')
    expect(wrapper.emitted('retry-print')?.[0]).toEqual(['p1'])
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

  it('maps the clicked checkbox to its own source line past a fence and a blockquote', async () => {
    // The fenced literal renders as code (no checkbox) and the quoted task renders as one, so a
    // DOM index only lands on the right line if both sides share the renderer's enumeration.
    const content = '```\n- [ ] fenced\n```\n\n> - [ ] quoted\n\n- [ ] plain'
    const wrapper = mountCard({ memo: memo(content) })
    const boxes = wrapper.findAll('input.task-list-item-checkbox')
    expect(boxes).toHaveLength(2)
    await boxes[0]!.trigger('click')
    await boxes[1]!.trigger('click')
    expect(wrapper.emitted('task-toggle')).toEqual([
      [{ uid: 'm1', content: '```\n- [ ] fenced\n```\n\n> - [x] quoted\n\n- [ ] plain' }],
      [{ uid: 'm1', content: '```\n- [ ] fenced\n```\n\n> - [ ] quoted\n\n- [x] plain' }],
    ])
  })

  it('renders a lone photo at its own ratio, content width, capped in height', () => {
    const wrapper = mountCard({
      memo: memo('one big photo', [], [imageAtt('img1', { width: 3000, height: 2000 })]),
    })
    const single = wrapper.find('[data-test="media-single"]')
    expect(single.exists()).toBe(true)
    expect(wrapper.find('[data-test="media-grid"]').exists()).toBe(false) // not the old square grid
    // Full column width, but never wider than (cap × ratio) — so the cap narrows the box (35 × 1.5)
    // instead of cropping the photo.
    expect(single.attributes('style')).toContain('width: 100%')
    expect(single.attributes('style')).toContain('max-width: 52.500rem')
    expect(wrapper.find('[data-test="media-tile"]').attributes('style')).toContain('aspect-ratio: 1.5')
  })

  it('renders a lone portrait photo taller than wide (ratio follows the photo)', () => {
    const wrapper = mountCard({
      memo: memo('portrait', [], [imageAtt('img1', { width: 2000, height: 3000 })]),
    })
    // 2:3 at a 35rem height cap → at most 23.333rem wide (a portrait must not run the full column)
    expect(wrapper.find('[data-test="media-single"]').attributes('style')).toContain(
      'max-width: 23.333rem',
    )
    expect(wrapper.find('[data-test="media-tile"]').attributes('style')).toContain(
      'aspect-ratio: 0.6666',
    )
  })

  it('sizes a lone portrait against its capped width, so it loads the thumb and not the mobile tier', () => {
    const wrapper = mountCard({
      memo: memo('portrait', [], [imageAtt('img1', { width: 3000, height: 4000 })]),
    })
    // The box is 420px wide (3:4 under the 35rem cap), not the 832px column — advertising the
    // column would pull the 1536w candidate for a render the 600w thumb covers.
    const img = wrapper.find('[data-test="tile-image"]')
    expect(img.attributes('sizes')).toBe('(min-width: 484px) 420px, calc(100vw - 64px)')
    expect(img.attributes('srcset')).toBe(
      '/api/attachments/img1/thumb 600w, /api/attachments/img1/mobile 1536w',
    )
  })

  it('falls back to a 4:3 box for a lone photo whose dims are not known yet', () => {
    const wrapper = mountCard({ memo: memo('legacy photo', [], [imageAtt('img1')]) })
    expect(wrapper.find('[data-test="media-single"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="media-tile"]').attributes('style')).toContain(
      'aspect-ratio: 1.333',
    )
  })

  it('renders two photos as a uniform two-column grid, not at their own ratios', () => {
    const wrapper = mountCard({
      memo: memo(
        'two',
        [],
        [
          imageAtt('img1', { width: 3000, height: 2000 }),
          imageAtt('img2', { width: 2000, height: 3000 }),
        ],
      ),
    })
    const grid = wrapper.find('[data-test="media-grid"]')
    expect(grid.exists()).toBe(true)
    expect(grid.classes()).toContain('grid-cols-2')
    const tiles = wrapper.findAll('[data-test="media-tile"]')
    expect(tiles).toHaveLength(2)
    // Both boxes share one ratio (each photo crops into it) despite opposite orientations.
    expect(tiles[0].attributes('style')).toBe(tiles[1].attributes('style'))
    expect(tiles[0].attributes('style')).not.toContain('1.5')
  })

  it('packs three or more photos into justified rows scaled to the column width', async () => {
    stubResizeObserver(400)
    const wrapper = mountCard({
      memo: memo(
        'three',
        [],
        [
          imageAtt('img1', { width: 400, height: 200 }), // 2:1
          imageAtt('img2', { width: 200, height: 200 }), // 1:1
          imageAtt('img3', { width: 200, height: 400 }), // 1:2
        ],
      ),
    })
    await nextTick()
    expect(wrapper.find('[data-test="media-justified"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="media-grid"]').exists()).toBe(false)
    const tiles = wrapper.findAll('[data-test="media-tile"]')
    expect(tiles).toHaveLength(3)
    const px = (tile: (typeof tiles)[number], prop: 'width' | 'height') =>
      Number(new RegExp(`${prop}: ([\\d.]+)px`).exec(tile.attributes('style') ?? '')?.[1])
    // Same row height, widths in proportion to each photo's ratio (2:1 widest, 1:2 narrowest).
    expect(px(tiles[0], 'height')).toBeCloseTo(px(tiles[1], 'height'), 3)
    expect(px(tiles[0], 'width')).toBeGreaterThan(px(tiles[1], 'width'))
    expect(px(tiles[1], 'width')).toBeGreaterThan(px(tiles[2], 'width'))
    wrapper.unmount()
  })

  it('opens the lightbox at the clicked image (composable open with the right index)', async () => {
    lightboxOpen.mockClear()
    const wrapper = mountCard({ memo: memo('pics', [], [imageAtt('img1'), imageAtt('img2')]) })
    await wrapper.find('[data-test="tile-image"]').trigger('click')
    expect(lightboxOpen).toHaveBeenCalledTimes(1)
    const [atts, index] = lightboxOpen.mock.calls[0]
    expect(atts).toHaveLength(2)
    expect(index).toBe(0) // the first attachment was clicked
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

  // MEMNG-46 wiring pins: both edit-mode queue clears must go through the composable's release path
  // — `editUploads.length = 0` empties the array without ever revoking the preview object URLs.
  describe('edit-upload queue release', () => {
    async function openEditWithStagedImage(wrapper: ReturnType<typeof mountCard>) {
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
      const editItem = await openAction(wrapper, 'memo-action-edit')
      editItem.click()
      await nextTick()
      const input = wrapper.find('[data-test="edit-file-input"]')
      const file = new File([new Uint8Array(3)], 'new.png', { type: 'image/png' })
      Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
      await input.trigger('change')
      await nextTick()
    }

    it('revokes staged preview object URLs when the edit is saved', async () => {
      vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock/new.png')
      const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
      const wrapper = mountCard({ memo: memo('with staged image') })
      await openEditWithStagedImage(wrapper)

      await wrapper.find('[data-test="edit-save"]').trigger('click')
      await nextTick()

      expect(revoke).toHaveBeenCalledWith('blob:mock/new.png')
      wrapper.unmount()
    })

    it('revokes staged preview object URLs when the edit is cancelled', async () => {
      vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock/new.png')
      const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
      const wrapper = mountCard({ memo: memo('with staged image') })
      await openEditWithStagedImage(wrapper)

      await wrapper.find('[data-test="edit-cancel"]').trigger('click')
      await nextTick()

      expect(wrapper.find('[data-test="memo-editor"]').exists()).toBe(false) // edit closed
      expect(revoke).toHaveBeenCalledWith('blob:mock/new.png') // …and the queue released
      wrapper.unmount()
    })
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

  it('Remove all emits ONE delete-attachments with every uid on confirm (parent deletes serially)', async () => {
    requireSpy.mockClear()
    const wrapper = mountCard({ memo: memo('pics', [], [imageAtt('img1'), imageAtt('img2')]) })
    const editItem = await openAction(wrapper, 'memo-action-edit')
    editItem.click()
    await nextTick()
    await wrapper.find('[data-test="edit-remove-all"]').trigger('click')
    expect(requireSpy).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('delete-attachments')).toBeUndefined() // nothing until confirmed
    const opts = requireSpy.mock.calls[0][0] as { accept: () => void }
    opts.accept()
    // a SINGLE bulk event (not N single deletes — those would fan out N concurrent DB connections)
    expect(wrapper.emitted('delete-attachments')).toEqual([[['img1', 'img2']]])
    expect(wrapper.emitted('delete-attachment')).toBeUndefined()
    wrapper.unmount()
  })

  it('hides Remove all in the editor when the memo has no photos', async () => {
    const wrapper = mountCard({ memo: memo('no pics') })
    const editItem = await openAction(wrapper, 'memo-action-edit')
    editItem.click()
    await nextTick()
    expect(wrapper.find('[data-test="edit-remove-all"]').exists()).toBe(false)
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
