import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import SelectButton from 'primevue/selectbutton'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import SettingsView from '@/views/SettingsView.vue'

const { mutate } = vi.hoisted(() => ({ mutate: vi.fn() }))
vi.mock('@/composables/useSession', () => ({
  useSession: () => ({ data: ref({ subject: 'fabian', display_name: 'Fabian', email: 'f@x' }) }),
}))
vi.mock('@/composables/usePushSubscription', () => ({
  usePushSubscription: () => ({
    supported: false,
    enabled: ref(false),
    busy: ref(false),
    enable: vi.fn(),
  }),
}))
vi.mock('@/composables/useSettings', () => ({
  useSettings: () => ({
    settings: ref({ default_visibility: 'PUBLIC', theme: 'dark', avatar: null }),
    update: { mutate },
  }),
}))
vi.mock('@/composables/useDarkMode', () => ({
  useDarkMode: () => ({ isDark: ref(true), toggle: vi.fn(), setDark: vi.fn() }),
}))

function mountSettings() {
  return mount(SettingsView, {
    global: { plugins: [PrimeVue, ConfirmationService, ToastService], stubs: { PatManager: { template: '<div />' } } },
  })
}

describe('SettingsView', () => {
  it('shows the default-visibility and theme controls reflecting the settings', () => {
    const wrapper = mountSettings()
    const vis = wrapper.find('[data-test="default-visibility"]')
    const theme = wrapper.find('[data-test="theme"]')
    expect(vis.exists()).toBe(true)
    expect(theme.exists()).toBe(true)
  })

  it('falls back to initials when no avatar is set', () => {
    const wrapper = mountSettings()
    expect(wrapper.find('[data-test="avatar-initials"]').text()).toBe('FA') // "Fabian"
    expect(wrapper.find('[data-test="avatar-img"]').exists()).toBe(false)
  })

  it('persists a default-visibility change', async () => {
    const wrapper = mountSettings()
    const visButton = wrapper
      .findAllComponents(SelectButton)
      .find((c) => c.attributes('data-test') === 'default-visibility')!
    visButton.vm.$emit('update:modelValue', 'PRIVATE')
    await wrapper.vm.$nextTick()
    expect(mutate).toHaveBeenCalledWith({ default_visibility: 'PRIVATE' })
  })
})
