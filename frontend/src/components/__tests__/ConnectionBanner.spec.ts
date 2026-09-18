import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { computed, ref } from 'vue'

import ConnectionBanner from '@/components/ConnectionBanner.vue'

const status = ref<'reachable' | 'suspect' | 'unreachable'>('reachable')
const retryNow = vi.fn()

vi.mock('@/composables/useReachability', () => ({
  useReachability: () => ({
    status,
    unreachable: computed(() => status.value === 'unreachable'),
    retryNow,
  }),
}))

// A one-slot timer queue: the recovery notice is measured, never slept through.
function fakeTimers() {
  let pending: { fn: () => void; delayMs: number } | null = null
  return {
    timers: {
      set: (fn: () => void, delayMs: number) => {
        pending = { fn, delayMs }
        return 1
      },
      clear: () => {
        pending = null
      },
    },
    pending: () => pending,
    fire: () => {
      const due = pending
      pending = null
      due?.fn()
    },
  }
}

function mountWith(next: typeof status.value, timers = fakeTimers()) {
  status.value = next
  return { wrapper: mount(ConnectionBanner, { props: { timers: timers.timers } }), clock: timers }
}

describe('ConnectionBanner', () => {
  it('keeps a polite live region mounted even while all is well, and empty', () => {
    const { wrapper } = mountWith('reachable')
    const region = wrapper.find('[data-test="connection-live-region"]')
    expect(region.exists()).toBe(true)
    expect(region.attributes('role')).toBe('status')
    expect(region.attributes('aria-live')).toBe('polite')
    expect(region.text()).toBe('')
    expect(wrapper.find('[data-test="connection-banner"]').exists()).toBe(false)
  })

  it('stays hidden on a mere suspicion — one failed request is not news', () => {
    const { wrapper } = mountWith('suspect')
    expect(wrapper.find('[data-test="connection-banner"]').exists()).toBe(false)
  })

  it('puts the message inside that same region once the verdict is in', () => {
    const { wrapper } = mountWith('unreachable')
    expect(wrapper.find('[data-test="connection-banner"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="connection-live-region"]').text()).toContain(
      "Can't reach the server",
    )
  })

  it('confirms the recovery for about three seconds before clearing', async () => {
    const { wrapper, clock } = mountWith('unreachable')
    status.value = 'reachable'
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-test="connection-banner"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="connection-recovered"]').text()).toContain('Back online')
    expect(clock.pending()?.delayMs).toBe(3_000)

    clock.fire()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-test="connection-recovered"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="connection-live-region"]').text()).toBe('')
  })

  it('a second drop during the confirmation goes straight back to the bad news', async () => {
    const { wrapper, clock } = mountWith('unreachable')
    status.value = 'reachable'
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-test="connection-recovered"]').exists()).toBe(true)

    status.value = 'unreachable'
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-test="connection-recovered"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="connection-banner"]').exists()).toBe(true)
    // The stale timer must not fire later and blank a banner that is legitimately up.
    expect(clock.pending()).toBeNull()
  })

  it('tells the shell which rows of chrome it is drawing', async () => {
    const { wrapper, clock } = mountWith('reachable')
    expect(wrapper.emitted('occupied')?.at(-1)).toEqual([false])

    status.value = 'unreachable'
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('occupied')?.at(-1)).toEqual([true])

    status.value = 'reachable'
    await wrapper.vm.$nextTick()
    // Still occupied: the recovery notice is a row of chrome too.
    expect(wrapper.emitted('occupied')?.at(-1)).toEqual([true])

    clock.fire()
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('occupied')?.at(-1)).toEqual([false])
  })

  it('drops the pending recovery timer when it is unmounted', async () => {
    const { wrapper, clock } = mountWith('unreachable')
    status.value = 'reachable'
    await wrapper.vm.$nextTick()
    expect(clock.pending()).not.toBeNull()

    wrapper.unmount()
    expect(clock.pending()).toBeNull()
  })

  it('"Retry now" asks for an immediate probe', async () => {
    retryNow.mockClear()
    const { wrapper } = mountWith('unreachable')
    await wrapper.find('[data-test="connection-retry"]').trigger('click')
    expect(retryNow).toHaveBeenCalledTimes(1)
  })
})
