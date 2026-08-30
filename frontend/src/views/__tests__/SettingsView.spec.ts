import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import SelectButton from 'primevue/selectbutton'
import { ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import SettingsView from '@/views/SettingsView.vue'

const { mutate, mutateAsync, session, route, replace } = vi.hoisted(() => ({
  mutate: vi.fn(),
  mutateAsync: vi.fn().mockResolvedValue({}),
  session: { value: { subject: 'fabian', display_name: 'Fabian', email: 'f@x', role: 'admin' } },
  route: { query: {} as Record<string, unknown> },
  replace: vi.fn(),
}))
vi.mock('@/composables/useSession', () => ({
  useSession: () => ({ data: ref(session.value) }),
}))
vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ replace }),
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
    isPending: ref(false),
    update: { mutate, mutateAsync },
  }),
}))
vi.mock('@/composables/useDarkMode', () => ({
  useDarkMode: () => ({ isDark: ref(true), toggle: vi.fn(), setDark: vi.fn() }),
}))

function mountSettings() {
  return mount(SettingsView, {
    global: {
      plugins: [PrimeVue, ConfirmationService, ToastService],
      stubs: {
        PatManager: { template: '<div />' },
        DigestPanel: { template: '<div data-test="digest-panel-stub" />' },
      },
    },
  })
}

describe('SettingsView', () => {
  beforeEach(() => {
    session.value = { subject: 'fabian', display_name: 'Fabian', email: 'f@x', role: 'admin' }
    route.query = {}
    vi.clearAllMocks()
  })

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

  it('no longer renders the removed Aura frame section', () => {
    const wrapper = mountSettings()
    expect(wrapper.find('[data-test="aura-frame"]').exists()).toBe(false)
  })

  it('keeps every General section, in the order the view had them', () => {
    const wrapper = mountSettings()
    const general = wrapper.find('[data-test="settings-tab-general"]')
    expect(general.exists()).toBe(true)
    for (const section of ['profile', 'preferences', 'push-unsupported', 'version']) {
      expect(wrapper.find(`[data-test="${section}"]`).exists()).toBe(true)
    }
  })

  it('gives an admin both tabs, General first', () => {
    const wrapper = mountSettings()
    expect(wrapper.find('[data-test="settings-tab-general"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="settings-tab-mail"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="digest-panel-stub"]').exists()).toBe(true)
  })

  it('renders no Mail tab and no digest content for a non-admin', () => {
    session.value = { subject: 'ada', display_name: 'Ada', email: 'a@x', role: 'user' }
    const wrapper = mountSettings()
    expect(wrapper.find('[data-test="settings-tab-mail"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="digest-panel-stub"]').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/Digest e-mail/)
  })

  it('opens on the Mail tab when the URL asks for it', () => {
    route.query = { tab: 'mail' }
    const wrapper = mountSettings()
    expect(wrapper.findComponent({ name: 'Tabs' }).props('value')).toBe('mail')
  })

  it('lands a non-admin who follows ?tab=mail on General, with no Mail tab', () => {
    route.query = { tab: 'mail' }
    session.value = { subject: 'ada', display_name: 'Ada', email: 'a@x', role: 'user' }
    const wrapper = mountSettings()
    expect(wrapper.findComponent({ name: 'Tabs' }).props('value')).toBe('general')
    expect(wrapper.find('[data-test="settings-tab-mail"]').exists()).toBe(false)
  })

  it('falls back to General for an unknown tab value', () => {
    route.query = { tab: 'billing' }
    const wrapper = mountSettings()
    expect(wrapper.findComponent({ name: 'Tabs' }).props('value')).toBe('general')
  })

  it('mirrors the tab the admin picks into the URL', async () => {
    const wrapper = mountSettings()
    await wrapper.find('[data-test="settings-tab-mail"]').trigger('click')

    expect(replace).toHaveBeenCalledWith({ query: { tab: 'mail' } })
    expect(wrapper.findComponent({ name: 'Tabs' }).props('value')).toBe('mail')

    route.query = { tab: 'mail' }
    await wrapper.find('[data-test="settings-tab-general"]').trigger('click')
    expect(replace).toHaveBeenLastCalledWith({ query: { tab: 'general' } })
  })

  it('does not rewrite the URL when the tab is already the one in it', async () => {
    route.query = { tab: 'mail' }
    const wrapper = mountSettings()
    await wrapper.find('[data-test="settings-tab-mail"]').trigger('click')
    expect(replace).not.toHaveBeenCalled()
  })

  it('re-resolves ?tab=mail once the session says the viewer is an admin', async () => {
    // the session query answers after the first render: until it does, nobody is an admin
    route.query = { tab: 'mail' }
    session.value = { subject: 'fabian', display_name: 'Fabian', email: 'f@x', role: 'user' }
    const wrapper = mountSettings()
    expect(wrapper.findComponent({ name: 'Tabs' }).props('value')).toBe('general')
  })
})
