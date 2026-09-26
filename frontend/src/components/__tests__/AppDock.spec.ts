import { mount } from '@vue/test-utils'
import { defineComponent, h, markRaw, nextTick } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import AppDock from '@/components/AppDock.vue'
import { createDockSegment, DOCK_SEGMENT_KEY } from '@/composables/useDockSegment'
import { DOCK_SEGMENT_THRESHOLD } from '@/lib/dockState'

const Pill = markRaw(defineComponent({ render: () => h('div', { 'data-test': 'injected-pill' }) }))
const stub = { template: '<div />' }

async function mountDock(path: string, withSegment = true) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ['/', '/resources', '/places', '/search'].map((p) => ({ path: p, component: stub })),
  })
  await router.push(path)
  await router.isReady()
  const registration = createDockSegment()
  if (withSegment) registration.register(Pill)
  const wrapper = mount(AppDock, {
    attachTo: document.body,
    global: { plugins: [router], provide: { [DOCK_SEGMENT_KEY as symbol]: registration } },
  })
  return { wrapper, router }
}

async function scrollTo(y: number) {
  document.scrollingElement!.scrollTop = y
  window.dispatchEvent(new Event('scroll'))
  await nextTick()
}

const tabs = (w: Awaited<ReturnType<typeof mountDock>>['wrapper']) =>
  w.findAll('[data-test^="dock-tab-"]').map((t) => t.attributes('data-test'))
const pill = (w: Awaited<ReturnType<typeof mountDock>>['wrapper']) =>
  w.find('[data-test="injected-pill"]').exists()

describe('AppDock', () => {
  afterEach(async () => {
    document.scrollingElement!.scrollTop = 0
    document.body.innerHTML = ''
  })

  it('floats one glass row above the bottom inset, over the chrome layer', async () => {
    const { wrapper } = await mountDock('/')
    const dock = wrapper.find('[data-test="app-dock"]')
    expect(dock.classes()).toEqual(
      expect.arrayContaining(['fixed', 'z-40', 'glass', 'bottom-[calc(env(safe-area-inset-bottom)+12px)]']),
    )
  })

  it.each([
    ['/resources', 'dock-tab-library'],
    ['/', 'dock-tab-timeline'],
    ['/places', 'dock-tab-places'],
  ])('renders Library | Timeline | Places with the tab for %s active', async (path, active) => {
    const { wrapper } = await mountDock(path)
    expect(tabs(wrapper)).toEqual(['dock-tab-library', 'dock-tab-timeline', 'dock-tab-places'])
    const on = wrapper.findAll('[data-active="true"]').map((t) => t.attributes('data-test'))
    expect(on).toEqual([active])
    expect(wrapper.find(`[data-test="${active}"]`).attributes('aria-current')).toBe('page')
    expect(wrapper.find('[data-test="dock-tab-library"]').attributes('href')).toBe('/resources')
    expect(wrapper.find('[data-test="dock-tab-timeline"]').attributes('href')).toBe('/')
    expect(wrapper.find('[data-test="dock-tab-places"]').attributes('href')).toBe('/places')
  })

  it('carries no search control of any kind', async () => {
    const { wrapper } = await mountDock('/resources')
    await scrollTo(DOCK_SEGMENT_THRESHOLD + 200)
    for (const html of [wrapper.html()]) {
      expect(html).not.toMatch(/search/i)
    }
    await scrollTo(0)
    expect(wrapper.html()).not.toMatch(/search/i)
    expect(wrapper.findAll('a, button').length).toBeGreaterThan(0) // control: the tabs are there
  })

  it('swaps the tabs for the Library icon plus the injected pill after a scroll, and back at the top', async () => {
    const { wrapper } = await mountDock('/resources')
    expect(pill(wrapper)).toBe(false)

    await scrollTo(DOCK_SEGMENT_THRESHOLD + 1)
    expect(pill(wrapper)).toBe(true)
    expect(wrapper.find('[data-test="dock-library-toggle"]').exists()).toBe(true)
    expect(tabs(wrapper)).toEqual([])

    await scrollTo(0)
    expect(pill(wrapper)).toBe(false)
    expect(tabs(wrapper)).toHaveLength(3)
  })

  it('toggles between tabs and pill on a tap of the active Library tab, and keeps the query', async () => {
    const { wrapper, router } = await mountDock('/resources')
    await router.replace('/resources?type=video')
    await wrapper.find('[data-test="dock-tab-library"]').trigger('click')
    await nextTick()
    expect(pill(wrapper)).toBe(true)
    // The tap toggles; it must not navigate to the bare route and drop the filter.
    expect(router.currentRoute.value.fullPath).toBe('/resources?type=video')

    await wrapper.find('[data-test="dock-library-toggle"]').trigger('click')
    await nextTick()
    expect(pill(wrapper)).toBe(false)
  })

  it.each(['/', '/places'])('never shows the pill on %s, scrolled or tapped', async (path) => {
    const { wrapper } = await mountDock(path)
    await scrollTo(5000)
    expect(pill(wrapper)).toBe(false)
    const active = path === '/' ? 'dock-tab-timeline' : 'dock-tab-places'
    await wrapper.find(`[data-test="${active}"]`).trigger('click')
    await nextTick()
    expect(pill(wrapper)).toBe(false)
    expect(tabs(wrapper)).toHaveLength(3)
  })

  it('returns to the tabs when the Library is left in segment mode', async () => {
    const { wrapper, router } = await mountDock('/resources')
    await scrollTo(500)
    expect(pill(wrapper)).toBe(true)
    await router.push('/places')
    await nextTick()
    await router.push('/resources')
    await nextTick()
    expect(pill(wrapper)).toBe(false)
  })

  it('removes the very scroll listener it added, once it is gone', async () => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    try {
      const { wrapper } = await mountDock('/resources')
      const added = add.mock.calls.filter(([type]) => type === 'scroll')
      expect(added).toHaveLength(1)
      const [, handler, options] = added[0]!
      expect(options).toEqual({ passive: true })

      wrapper.unmount()

      const removed = remove.mock.calls.filter(([type]) => type === 'scroll')
      expect(removed.map(([, h]) => h)).toContain(handler)
    } finally {
      add.mockRestore()
      remove.mockRestore()
    }
  })
})
