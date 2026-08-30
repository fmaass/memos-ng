import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import SelectButton from 'primevue/selectbutton'
import { nextTick, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import SettingsView from '@/views/SettingsView.vue'

const { mutate, mutateAsync, replace } = vi.hoisted(() => ({
  mutate: vi.fn(),
  mutateAsync: vi.fn().mockResolvedValue({}),
  replace: vi.fn(),
}))

// Reactive on purpose: the session ARRIVES (a query resolving after the first render) and the URL
// CHANGES (the view's own router.replace). A snapshot object would make both of those tests
// unfalsifiable — they would pass against a view that never re-resolved anything.
const { session, route } = await vi.hoisted(async () => {
  const { reactive, ref: vueRef } = await import('vue')
  return {
    session: vueRef<Record<string, unknown> | undefined>(undefined),
    route: reactive({ query: {} as Record<string, unknown> }),
  }
})

vi.mock('@/composables/useSession', () => ({ useSession: () => ({ data: session }) }))
vi.mock('vue-router', () => ({ useRoute: () => route, useRouter: () => ({ replace }) }))
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

const ADMIN = { subject: 'fabian', display_name: 'Fabian', email: 'f@x', role: 'admin' }
const MEMBER = { subject: 'ada', display_name: 'Ada', email: 'a@x', role: 'user' }

function mountSettings() {
  return mount(SettingsView, {
    global: {
      plugins: [PrimeVue, ConfirmationService, ToastService],
      stubs: {
        PatManager: { template: '<div data-test="pat-manager" />' },
        DigestPanel: { template: '<div data-test="digest-panel-stub" />' },
        ExportPanel: { template: '<div data-test="export-panel-stub" />' },
      },
    },
  })
}

const activeTab = (wrapper: ReturnType<typeof mountSettings>) =>
  wrapper.findComponent({ name: 'Tabs' }).props('value')

describe('SettingsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    session.value = { ...ADMIN }
    route.query = {}
    // The router really does rewrite the URL, so the view sees its own correction come back.
    replace.mockImplementation(({ query }: { query: Record<string, unknown> }) => {
      route.query = { ...query }
    })
  })

  it('shows the default-visibility and theme controls reflecting the settings', () => {
    const wrapper = mountSettings()
    expect(wrapper.find('[data-test="default-visibility"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="theme"]').exists()).toBe(true)
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
    const html = mountSettings().html()
    const at = (test: string) => {
      const index = html.indexOf(`data-test="${test}"`)
      expect(index, `${test} is missing from the General tab`).toBeGreaterThan(-1)
      return index
    }
    // Profile → Preferences → Access tokens → Notifications, the order SettingsView.vue rendered
    // before the tabs existed. Access tokens is PatManager, so the stub carries a marker.
    expect(at('profile')).toBeLessThan(at('preferences'))
    expect(at('preferences')).toBeLessThan(at('pat-manager'))
    expect(at('pat-manager')).toBeLessThan(at('push-unsupported'))
  })

  it('gives an admin all three tabs, General first and Export last', () => {
    const wrapper = mountSettings()
    const html = wrapper.html()
    expect(wrapper.find('[data-test="settings-tab-general"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="settings-tab-mail"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="settings-tab-export"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="digest-panel-stub"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="export-panel-stub"]').exists()).toBe(true)
    expect(html.indexOf('settings-tab-general')).toBeLessThan(html.indexOf('settings-tab-mail'))
    expect(html.indexOf('settings-tab-mail')).toBeLessThan(html.indexOf('settings-tab-export'))
  })

  it('renders no Mail tab and no digest content for a non-admin', () => {
    session.value = { ...MEMBER }
    const wrapper = mountSettings()
    expect(wrapper.find('[data-test="settings-tab-mail"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="digest-panel-stub"]').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/Digest e-mail/)
  })

  it('renders no Export tab and no export content for a non-admin — not a disabled stub', () => {
    session.value = { ...MEMBER }
    const wrapper = mountSettings()
    expect(wrapper.find('[data-test="settings-tab-export"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="export-panel-stub"]').exists()).toBe(false)
    expect(wrapper.html()).not.toMatch(/Export/)
  })

  it('opens on the Mail tab when the URL asks for it, and leaves that URL alone', () => {
    route.query = { tab: 'mail' }
    const wrapper = mountSettings()
    expect(activeTab(wrapper)).toBe('mail')
    expect(replace).not.toHaveBeenCalled()
  })

  it('lands a non-admin who follows ?tab=mail on General and corrects the URL', async () => {
    route.query = { tab: 'mail' }
    session.value = { ...MEMBER }
    const wrapper = mountSettings()
    await nextTick()

    expect(activeTab(wrapper)).toBe('general')
    expect(wrapper.find('[data-test="settings-tab-mail"]').exists()).toBe(false)
    expect(replace).toHaveBeenCalledWith({ query: { tab: 'general' } })
    expect(route.query.tab).toBe('general') // the URL no longer states a tab that is not shown
    expect(replace).toHaveBeenCalledTimes(1) // and the correction does not re-trigger itself
  })

  it('rewrites an unknown or empty ?tab= to the tab it actually shows', async () => {
    route.query = { tab: 'billing' }
    const wrapper = mountSettings()
    await nextTick()
    expect(activeTab(wrapper)).toBe('general')
    expect(route.query.tab).toBe('general')

    replace.mockClear()
    route.query = { tab: null } // a bare `?tab`
    await nextTick()
    expect(replace).toHaveBeenCalledWith({ query: { tab: 'general' } })
    expect(route.query.tab).toBe('general')
  })

  it('leaves a URL with no tab at all alone', async () => {
    const wrapper = mountSettings()
    await nextTick()
    expect(activeTab(wrapper)).toBe('general')
    expect(replace).not.toHaveBeenCalled()
    expect(route.query.tab).toBeUndefined()
  })

  it('mirrors the tab the admin picks into the URL, both ways', async () => {
    const wrapper = mountSettings()
    await wrapper.find('[data-test="settings-tab-mail"]').trigger('click')
    expect(replace).toHaveBeenCalledWith({ query: { tab: 'mail' } })
    expect(activeTab(wrapper)).toBe('mail')

    await wrapper.find('[data-test="settings-tab-general"]').trigger('click')
    expect(replace).toHaveBeenLastCalledWith({ query: { tab: 'general' } })
    await nextTick()
    expect(activeTab(wrapper)).toBe('general')
  })

  it('does not rewrite the URL when the tab is already the one in it', async () => {
    route.query = { tab: 'mail' }
    const wrapper = mountSettings()
    await wrapper.find('[data-test="settings-tab-mail"]').trigger('click')
    expect(replace).not.toHaveBeenCalled()
  })

  it('promotes ?tab=mail once the session resolves and says the viewer is an admin', async () => {
    // The session query answers AFTER the first render: until it does, nobody is an admin.
    session.value = undefined
    route.query = { tab: 'mail' }
    const wrapper = mountSettings()
    await nextTick()
    // NOT General: rendering it here and flipping a tick later is the flash the guard exists to
    // stop. While the answer is unknown the view shows no panel at all.
    expect(activeTab(wrapper)).toBe('')
    expect(wrapper.find('[data-test="profile"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="settings-tab-mail"]').exists()).toBe(false)

    session.value = { ...ADMIN } // the same ref the view is watching
    await nextTick()
    await nextTick()
    expect(wrapper.find('[data-test="settings-tab-mail"]').exists()).toBe(true)
    expect(activeTab(wrapper)).toBe('mail')
  })
  it('opens on the Export tab when the URL asks for it, and leaves that URL alone', () => {
    route.query = { tab: 'export' }
    const wrapper = mountSettings()
    expect(activeTab(wrapper)).toBe('export')
    expect(wrapper.find('[data-test="export-panel-stub"]').exists()).toBe(true)
    expect(replace).not.toHaveBeenCalled()
  })

  it('lands a non-admin who follows ?tab=export on General and corrects the URL', async () => {
    route.query = { tab: 'export' }
    session.value = { ...MEMBER }
    const wrapper = mountSettings()
    await nextTick()

    expect(activeTab(wrapper)).toBe('general')
    expect(wrapper.find('[data-test="settings-tab-export"]').exists()).toBe(false)
    expect(replace).toHaveBeenCalledWith({ query: { tab: 'general' } })
    expect(route.query.tab).toBe('general')
    expect(replace).toHaveBeenCalledTimes(1)
  })

  it('mirrors the Export tab the admin picks into the URL, and back again', async () => {
    const wrapper = mountSettings()
    await wrapper.find('[data-test="settings-tab-export"]').trigger('click')
    expect(replace).toHaveBeenCalledWith({ query: { tab: 'export' } })
    expect(activeTab(wrapper)).toBe('export')

    await wrapper.find('[data-test="settings-tab-mail"]').trigger('click')
    expect(replace).toHaveBeenLastCalledWith({ query: { tab: 'mail' } })
    await nextTick()
    expect(activeTab(wrapper)).toBe('mail')
  })

  it('promotes ?tab=export once the session resolves and says the viewer is an admin', async () => {
    session.value = undefined
    route.query = { tab: 'export' }
    const wrapper = mountSettings()
    await nextTick()
    expect(activeTab(wrapper)).toBe('')
    expect(wrapper.find('[data-test="settings-tab-export"]').exists()).toBe(false)
    expect(replace).not.toHaveBeenCalled() // an unknown role must not correct the URL

    session.value = { ...ADMIN }
    await nextTick()
    await nextTick()
    expect(wrapper.find('[data-test="settings-tab-export"]').exists()).toBe(true)
    expect(activeTab(wrapper)).toBe('export')
    expect(route.query.tab).toBe('export') // the deep link survived the wait
  })
  it('renders no panel content at all while an admin deep link is still unanswerable', async () => {
    // The whole point of the pending window: an admin following ?tab=export must not be shown
    // General's Profile/Preferences/tokens for a tick and then have them replaced.
    session.value = undefined
    route.query = { tab: 'export' }
    const wrapper = mountSettings()
    await nextTick()

    expect(wrapper.find('[data-test="profile"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="preferences"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="pat-manager"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="digest-panel-stub"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="export-panel-stub"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="settings-tab-general"]').exists()).toBe(true) // the strip stays

    session.value = { ...ADMIN }
    await nextTick()
    await nextTick()
    expect(wrapper.find('[data-test="export-panel-stub"]').exists()).toBe(true)
    // General's content comes back with the rest — PrimeVue mounts every panel — but it was never
    // the ACTIVE one, which is what a reader would have seen.
    expect(activeTab(wrapper)).toBe('export')
  })

  it('still shows General immediately when the URL asks for no admin tab', async () => {
    // The neutral state is only for an unanswerable question. A plain /settings, or ?tab=general,
    // is answerable without the role, and waiting there would be its own regression.
    session.value = undefined
    const wrapper = mountSettings()
    await nextTick()
    expect(activeTab(wrapper)).toBe('general')
    expect(wrapper.find('[data-test="profile"]').exists()).toBe(true)

    route.query = { tab: 'billing' } // unknown, and unknown means General for everyone
    await nextTick()
    expect(activeTab(wrapper)).toBe('general')
    expect(wrapper.find('[data-test="profile"]').exists()).toBe(true)
  })
})
