import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import SelectButton from 'primevue/selectbutton'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import SettingsView from '@/views/SettingsView.vue'

const { mutate, mutateAsync, frameTestMutateAsync, persistedFrame } = vi.hoisted(() => ({
  mutate: vi.fn(),
  mutateAsync: vi.fn().mockResolvedValue({}),
  frameTestMutateAsync: vi.fn().mockResolvedValue({ ok: true, detail: 'Accepted — check your frame.' }),
  // Persisted frame settings the mocked useSettings reports; the component seeds its edit buffers
  // from these, so buffers match persisted on mount (clean → test button enabled).
  persistedFrame: { to: 'f@send.auraframes.com' as string | null, from: 'me@me.com' as string | null },
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
        frame_send_to: persistedFrame.to, frame_send_from: persistedFrame.from,
      }),
      update: { mutate, mutateAsync },
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

  it('saves the Aura frame addresses via mutateAsync and confirms with a banner', async () => {
    mutateAsync.mockResolvedValueOnce({})
    const wrapper = mountSettings()
    expect(wrapper.find('[data-test="aura-frame"]').exists()).toBe(true)
    await wrapper.find('[data-test="frame-save"]').trigger('click')
    await flushPromises()
    expect(mutateAsync).toHaveBeenCalledWith({
      frame_send_to: 'f@send.auraframes.com',
      frame_send_from: 'me@me.com',
    })
    expect(wrapper.find('[data-test="frame-test-result"]').text()).toContain('Saved')
  })

  it('surfaces a backend save error instead of silently swallowing it', async () => {
    // A 422 (e.g. a non-Aura address) must reach the user, not be lost to a fire-and-forget mutate.
    mutateAsync.mockRejectedValueOnce({
      detail: [{ msg: 'must be an @send.auraframes.com address' }],
    })
    const wrapper = mountSettings()
    await wrapper.find('[data-test="frame-send-to"]').setValue('me@icloud.com')
    await wrapper.find('[data-test="frame-save"]').trigger('click')
    await flushPromises()
    const banner = wrapper.find('[data-test="frame-test-result"]')
    expect(banner.text()).toContain('@send.auraframes.com')
    expect(banner.classes()).toContain('text-red-500')
  })

  it('sends a test photo when edits are saved (buffers match persisted)', async () => {
    const wrapper = mountSettings()
    const testBtn = wrapper.find('[data-test="frame-test"]')
    expect(testBtn.attributes('disabled')).toBeUndefined() // clean → enabled
    await testBtn.trigger('click')
    await flushPromises()
    expect(frameTestMutateAsync).toHaveBeenCalled()
    expect(wrapper.find('[data-test="frame-test-result"]').text()).toContain('check your frame')
  })

  it('disables Send test photo while edits differ from the saved address (dirty)', async () => {
    const wrapper = mountSettings()
    // Change the buffer away from the persisted value — testing now would report on the STALE
    // saved address, so the button must be disabled until the edit is saved.
    await wrapper.find('[data-test="frame-send-to"]').setValue('other@send.auraframes.com')
    expect(wrapper.find('[data-test="frame-test"]').attributes('disabled')).toBeDefined()
  })
})
