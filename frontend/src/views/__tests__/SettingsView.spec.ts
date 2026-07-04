import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import SelectButton from 'primevue/selectbutton'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import SettingsView from '@/views/SettingsView.vue'

const { mutate, frameTestMutateAsync } = vi.hoisted(() => ({
  mutate: vi.fn(),
  frameTestMutateAsync: vi.fn().mockResolvedValue({ ok: true, detail: 'Accepted — check your frame.' }),
}))
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
vi.mock('@/composables/useSettings', async () => {
  const actual = await vi.importActual<typeof import('@/composables/useSettings')>(
    '@/composables/useSettings',
  )
  return {
    ...actual, // keep the real pure frameTestMessage
    useSettings: () => ({
      settings: ref({
        default_visibility: 'PUBLIC', theme: 'dark', avatar: null,
        frame_send_to: null, frame_send_from: null,
      }),
      update: { mutate },
      frameTest: { mutateAsync: frameTestMutateAsync, isPending: ref(false) },
    }),
  }
})
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

  it('saves the Aura frame addresses and shows the test-send result', async () => {
    const wrapper = mountSettings()
    expect(wrapper.find('[data-test="aura-frame"]').exists()).toBe(true)
    // PrimeVue InputText forwards data-test to its <input>; setValue drives the v-model buffer.
    await wrapper.find('[data-test="frame-send-to"]').setValue('f@send.auraframes.com')
    await wrapper.find('[data-test="frame-send-from"]').setValue('me@me.com')
    await wrapper.find('[data-test="frame-save"]').trigger('click')
    expect(mutate).toHaveBeenCalledWith({
      frame_send_to: 'f@send.auraframes.com',
      frame_send_from: 'me@me.com',
    })

    await wrapper.find('[data-test="frame-test"]').trigger('click')
    await flushPromises()
    expect(frameTestMutateAsync).toHaveBeenCalled()
    expect(wrapper.find('[data-test="frame-test-result"]').text()).toContain('check your frame')
  })
})
