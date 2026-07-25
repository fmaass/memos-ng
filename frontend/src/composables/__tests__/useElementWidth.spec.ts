import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, nextTick, ref } from 'vue'

import { useElementWidth } from '@/composables/useElementWidth'

const disconnected = vi.fn()

// happy-dom reports clientWidth 0 for every element, so drive the width through a ResizeObserver
// stub — the same path a real browser takes on resize.
function stubResizeObserver(width: number) {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(private cb: ResizeObserverCallback) {}
      observe() {
        this.cb([{ contentRect: { width } } as ResizeObserverEntry], this as ResizeObserver)
      }
      disconnect() {
        disconnected()
      }
      unobserve() {}
    },
  )
}

const Host = defineComponent({
  setup() {
    const el = ref<HTMLElement | null>(null)
    const width = useElementWidth(el)
    return { el, width }
  },
  template: '<div ref="el">{{ width }}</div>',
})

describe('useElementWidth', () => {
  beforeEach(() => {
    disconnected.mockClear()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('reports the observed element width', async () => {
    stubResizeObserver(640)
    const wrapper = mount(Host)
    await nextTick()
    expect(wrapper.text()).toBe('640')
  })

  it('stops observing when the host unmounts', () => {
    stubResizeObserver(640)
    mount(Host).unmount()
    expect(disconnected).toHaveBeenCalled()
  })

  it('stays at the mount-time width where ResizeObserver is unavailable', async () => {
    vi.stubGlobal('ResizeObserver', undefined)
    const wrapper = mount(Host)
    await nextTick()
    expect(wrapper.text()).toBe('0') // happy-dom clientWidth; no crash without the observer
  })
})
