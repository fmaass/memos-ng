import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import JustifiedGallery from '@/components/JustifiedGallery.vue'

type Status = 'ready' | 'pending' | 'uploading' | 'processing' | 'failed'
function item(uid: string, over: { mime?: string; status?: Status; mine?: boolean } = {}) {
  return {
    uid,
    mime: over.mime ?? 'image/jpeg',
    status: over.status ?? 'ready',
    blurhash: null,
    width: 400,
    height: 300,
    mine: over.mine ?? true,
    filename: `${uid}.jpg`,
    thumbnailUrl: `/api/attachments/${uid}/thumb`,
    previewUrl: `/api/attachments/${uid}/mobile`,
    webUrl: `/api/attachments/${uid}/web`,
    posterUrl: `/api/attachments/${uid}/poster`,
  }
}

function mountGallery(items: ReturnType<typeof item>[], selected: string[] = [], selecting = true) {
  return mount(JustifiedGallery, {
    props: { items, targetHeight: 200, selecting, selected: new Set(selected) },
  })
}
const control = (w: ReturnType<typeof mountGallery>, uid: string) =>
  w.find(`[data-uid="${uid}"] [data-test="tile-select"]`)

describe('JustifiedGallery in select mode', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(private cb: ResizeObserverCallback) {}
        observe() {
          this.cb([{ contentRect: { width: 400 } } as ResizeObserverEntry], this as ResizeObserver)
        }
        disconnect() {}
        unobserve() {}
      },
    )
  })
  afterEach(() => vi.unstubAllGlobals())

  it('a tap emits select with the uid and never open', async () => {
    const wrapper = mountGallery([item('a'), item('v', { mime: 'video/mp4' })])
    await flushPromises()
    await control(wrapper, 'a').trigger('click')
    await control(wrapper, 'v').trigger('click')
    expect(wrapper.emitted('select')).toEqual([['a'], ['v']])
    expect(wrapper.emitted('open')).toBeUndefined()
  })

  it('the open buttons underneath cannot open while selecting (the gate at the emit site)', async () => {
    const wrapper = mountGallery([item('a'), item('v', { mime: 'video/mp4' })])
    await flushPromises()
    // happy-dom does not honour `inert`, so this reaches the tile's own handlers: the gallery's
    // branch is what refuses them.
    await wrapper.find('[data-test="tile-open"]').trigger('click')
    await wrapper.find('[data-test="tile-video"]').trigger('click')
    expect(wrapper.emitted('open')).toBeUndefined()
    expect(wrapper.emitted('select')).toBeUndefined()
  })

  it('makes the tile content inert while selecting, so Tab reaches only the selection control', async () => {
    const wrapper = mountGallery([item('a')])
    await flushPromises()
    expect(wrapper.find('[data-test="tile-content"]').attributes('inert')).toBeDefined()
    const outside = mountGallery([item('a')], [], false)
    await flushPromises()
    expect(outside.find('[data-test="tile-content"]').attributes('inert')).toBeUndefined()
  })

  it.each<[string, ReturnType<typeof item>]>([
    ['ready image', item('x')],
    ['ready video', item('x', { mime: 'video/mp4' })],
    ['pending placeholder', item('x', { status: 'pending' })],
    ['uploading placeholder', item('x', { status: 'uploading' })],
    ['processing video', item('x', { mime: 'video/mp4', status: 'processing' })],
    ['failed', item('x', { status: 'failed' })],
  ])('a %s tile renders exactly one native selection button covering it', async (_, att) => {
    const wrapper = mountGallery([att])
    await flushPromises()
    const controls = wrapper.findAll('[data-test="tile-select"]')
    expect(controls).toHaveLength(1)
    expect(controls[0]!.element.tagName).toBe('BUTTON')
    expect(controls[0]!.attributes('type')).toBe('button')
    expect(controls[0]!.classes()).toEqual(expect.arrayContaining(['absolute', 'inset-0']))
    await controls[0]!.trigger('click')
    expect(wrapper.emitted('select')).toEqual([['x']])
  })

  it('a tile whose thumbnail failed to load is still selectable', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountGallery([item('a')])
      await flushPromises()
      for (let i = 0; i < 3; i++) {
        await wrapper.find('[data-test="tile-image"]').trigger('error')
        await vi.advanceTimersByTimeAsync(5_000)
      }
      expect(wrapper.find('[data-test="tile-failed"]').exists()).toBe(true)
      await control(wrapper, 'a').trigger('click')
      expect(wrapper.emitted('select')).toEqual([['a']])
    } finally {
      vi.useRealTimers()
    }
  })

  it('native button semantics: one click is one selection change, with aria-pressed and a label naming the item', async () => {
    const wrapper = mountGallery([item('a'), item('b')])
    await flushPromises()
    const a = control(wrapper, 'a')
    expect(a.attributes('aria-pressed')).toBe('false')
    expect(a.attributes('aria-label')).toBe('a.jpg')
    expect(a.attributes('aria-checked')).toBeUndefined()
    await a.trigger('click')
    expect(wrapper.emitted('select')).toEqual([['a']])
    // The parent owns the set: hand the change back as it would, and the control follows it.
    await wrapper.setProps({ selected: new Set(['a']) })
    expect(control(wrapper, 'a').attributes('aria-pressed')).toBe('true')
    expect(control(wrapper, 'b').attributes('aria-pressed')).toBe('false')
    expect(wrapper.findAll('[aria-pressed="true"]')).toHaveLength(1)
    expect(wrapper.emitted('open')).toBeUndefined()
  })

  it('a selected tile shows the check badge; an unselected one does not and is not dimmed', async () => {
    const wrapper = mountGallery([item('a'), item('b')], ['a'])
    await flushPromises()
    expect(wrapper.find('[data-uid="a"] [data-test="tile-check"]').exists()).toBe(true)
    expect(wrapper.find('[data-uid="b"] [data-test="tile-check"]').exists()).toBe(false)
    expect(wrapper.find('[data-uid="b"] [data-test="tile-content"]').classes()).not.toContain('opacity-40')
  })

  it('a tile that is not mine is dimmed and its control disabled: a tap emits nothing', async () => {
    const wrapper = mountGallery([item('f', { mine: false })])
    await flushPromises()
    const f = control(wrapper, 'f')
    expect(f.attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-test="tile-content"]').classes()).toContain('opacity-40')
    await f.trigger('click')
    expect(wrapper.emitted('select')).toBeUndefined()
    expect(wrapper.emitted('open')).toBeUndefined()
  })

  it('outside select mode there is no selection control and no pressed state, and a tap opens', async () => {
    const wrapper = mountGallery([item('a')], ['a'], false)
    await flushPromises()
    expect(wrapper.find('[data-test="tile-select"]').exists()).toBe(false)
    expect(wrapper.find('[aria-pressed]').exists()).toBe(false)
    expect(wrapper.find('[data-test="tile-check"]').exists()).toBe(false)
    await wrapper.find('[data-test="tile-open"]').trigger('click')
    expect(wrapper.emitted('open')).toEqual([[0]])
  })
})
