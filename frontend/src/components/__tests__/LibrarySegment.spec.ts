import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import LibrarySegment from '@/components/LibrarySegment.vue'

// The pill lives in the shell's dock, not in the view, so the route query is the only state the two
// share: the pill reads `view` from it and writes it back, keeping the filter fields as they are.
const { replace, route } = vi.hoisted(() => ({
  replace: vi.fn(),
  route: { query: {} as Record<string, string> },
}))
vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const live = reactive(route)
  return { useRouter: () => ({ replace }), useRoute: () => live }
})
const { useRoute } = await import('vue-router')

const mountPill = () => mount(LibrarySegment, { global: { plugins: [PrimeVue] } })
const pressed = (w: ReturnType<typeof mountPill>) =>
  w.findAll('button').map((b) => b.attributes('aria-pressed') === 'true')

describe('LibrarySegment', () => {
  beforeEach(() => {
    replace.mockReset()
    useRoute().query = {}
  })

  it('offers Years, Months and All, every one enabled', () => {
    const w = mountPill()
    const buttons = w.findAll('button')
    expect(buttons.map((b) => b.text())).toEqual(['Years', 'Months', 'All'])
    expect(buttons.map((b) => b.attributes('disabled'))).toEqual([undefined, undefined, undefined])
  })

  it('shows the segment the route names, All when it names none', async () => {
    const w = mountPill()
    expect(pressed(w)).toEqual([false, false, true])
    useRoute().query = { view: 'months', type: 'video' }
    await nextTick()
    expect(pressed(w)).toEqual([false, true, false])
  })

  it('writes a picked segment into the route and keeps the filter fields', async () => {
    useRoute().query = { type: 'video', tag: 'recap' }
    const w = mountPill()
    await w.findAll('button')[0]!.trigger('click')
    expect(replace).toHaveBeenCalledWith({ query: { type: 'video', tag: 'recap', view: 'years' } })
  })

  it('leaves All out of the URL', async () => {
    useRoute().query = { view: 'months', order: 'oldest' }
    const w = mountPill()
    await w.findAll('button')[2]!.trigger('click')
    expect(replace).toHaveBeenCalledWith({ query: { order: 'oldest' } })
  })
})
