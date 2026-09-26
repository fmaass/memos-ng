import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import LibrarySelectionToolbar from '@/components/LibrarySelectionToolbar.vue'

function mountBar(props: Partial<{ count: number; canShare: boolean; sharing: boolean; deleting: boolean }> = {}) {
  return mount(LibrarySelectionToolbar, {
    props: { count: 0, canShare: true, sharing: false, deleting: false, ...props },
  })
}
const find = (w: ReturnType<typeof mountBar>, t: string) => w.find(`[data-test="${t}"]`)

describe('LibrarySelectionToolbar', () => {
  it.each([
    [0, '0 Items Selected'],
    [1, '1 Item Selected'],
    [2, '2 Items Selected'],
  ])('says %i as "%s" in a polite, atomic status region', (count, text) => {
    const status = find(mountBar({ count }), 'selection-count')
    expect(status.text()).toBe(text)
    expect(status.attributes('role')).toBe('status')
    expect(status.attributes('aria-live')).toBe('polite')
    expect(status.attributes('aria-atomic')).toBe('true')
  })

  it('names the share and delete buttons with the count', () => {
    const w = mountBar({ count: 2 })
    expect(find(w, 'selection-share').attributes('aria-label')).toBe('Share 2 items')
    expect(find(w, 'selection-delete').attributes('aria-label')).toBe('Delete 2 items')
    const one = mountBar({ count: 1 })
    expect(find(one, 'selection-delete').attributes('aria-label')).toBe('Delete 1 item')
  })

  it('share on the left, count centred, delete on the right, all native round glass buttons', () => {
    const w = mountBar({ count: 1 })
    const order = w.findAll('[data-test^="selection-"]').map((e) => e.attributes('data-test'))
    expect(order).toEqual(['selection-share', 'selection-count', 'selection-delete'])
    for (const t of ['selection-share', 'selection-delete']) {
      const b = find(w, t)
      expect(b.element.tagName).toBe('BUTTON')
      expect(b.attributes('type')).toBe('button')
      expect(b.classes()).toEqual(expect.arrayContaining(['glass', 'rounded-full']))
    }
  })

  it('renders no share button where the platform cannot share files', () => {
    const w = mountBar({ count: 2, canShare: false })
    expect(find(w, 'selection-share').exists()).toBe(false)
    expect(find(w, 'selection-count').exists()).toBe(true)
  })

  it('disables delete and share at zero, and a tap then emits nothing', async () => {
    const w = mountBar({ count: 0 })
    for (const t of ['selection-share', 'selection-delete']) {
      expect(find(w, t).attributes('disabled')).toBeDefined()
      await find(w, t).trigger('click')
    }
    expect(w.emitted('share')).toBeUndefined()
    expect(w.emitted('delete')).toBeUndefined()
  })

  it('emits share and delete when there is something to act on', async () => {
    const w = mountBar({ count: 3 })
    await find(w, 'selection-share').trigger('click')
    await find(w, 'selection-delete').trigger('click')
    expect(w.emitted('share')).toHaveLength(1)
    expect(w.emitted('delete')).toHaveLength(1)
  })

  it('spins and holds the share button while the originals load', () => {
    const w = mountBar({ count: 1, sharing: true })
    const share = find(w, 'selection-share')
    expect(share.attributes('disabled')).toBeDefined()
    expect(share.attributes('aria-busy')).toBe('true')
    expect(share.find('.pi-spinner').exists()).toBe(true)
  })

  it('holds the delete button while a delete runs', () => {
    const del = find(mountBar({ count: 1, deleting: true }), 'selection-delete')
    expect(del.attributes('disabled')).toBeDefined()
    expect(del.attributes('aria-busy')).toBe('true')
  })
})
