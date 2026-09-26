import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useMediaQuery } from '@/composables/useMediaQuery'

function fakeMedia(initial: boolean) {
  const listeners = new Set<() => void>()
  const mql = {
    matches: initial,
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
  }
  vi.stubGlobal('matchMedia', vi.fn(() => mql))
  return {
    set(v: boolean) {
      mql.matches = v
      listeners.forEach((cb) => cb())
    },
    listeners,
  }
}

function host(query: string) {
  let matches!: ReturnType<typeof useMediaQuery>
  const wrapper = mount(
    defineComponent({
      setup() {
        matches = useMediaQuery(query)
        return () => h('div')
      },
    }),
  )
  return { wrapper, matches: () => matches.value }
}

describe('useMediaQuery', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('reports the query and follows its changes until unmount', () => {
    const media = fakeMedia(true)
    const { wrapper, matches } = host('(max-width: 767px)')
    expect(matches()).toBe(true)
    media.set(false)
    expect(matches()).toBe(false)
    wrapper.unmount()
    expect(media.listeners.size).toBe(0)
  })

  it('reads false where there is no matchMedia at all', () => {
    vi.stubGlobal('matchMedia', undefined)
    expect(host('(max-width: 767px)').matches()).toBe(false)
  })
})
