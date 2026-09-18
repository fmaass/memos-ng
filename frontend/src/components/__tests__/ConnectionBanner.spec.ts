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

function mountWith(next: typeof status.value) {
  status.value = next
  return mount(ConnectionBanner)
}

describe('ConnectionBanner', () => {
  it('shows nothing while the server is reachable', () => {
    const wrapper = mountWith('reachable')
    expect(wrapper.find('[data-test="connection-banner"]').exists()).toBe(false)
  })

  it('stays hidden on a mere suspicion — one failed request is not news', () => {
    const wrapper = mountWith('suspect')
    expect(wrapper.find('[data-test="connection-banner"]').exists()).toBe(false)
  })

  it('says the server cannot be reached once the verdict is in', () => {
    const wrapper = mountWith('unreachable')
    const banner = wrapper.find('[data-test="connection-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain("Can't reach the server")
  })

  it('is announced politely rather than interrupting the screen reader', () => {
    const banner = mountWith('unreachable').find('[data-test="connection-banner"]')
    expect(banner.attributes('role')).toBe('status')
    expect(banner.attributes('aria-live')).toBe('polite')
  })

  it('clears itself when the server comes back', async () => {
    const wrapper = mountWith('unreachable')
    expect(wrapper.find('[data-test="connection-banner"]').exists()).toBe(true)
    status.value = 'reachable'
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-test="connection-banner"]').exists()).toBe(false)
  })

  it('"Retry now" asks for an immediate probe', async () => {
    retryNow.mockClear()
    const wrapper = mountWith('unreachable')
    await wrapper.find('[data-test="connection-retry"]').trigger('click')
    expect(retryNow).toHaveBeenCalledTimes(1)
  })
})
