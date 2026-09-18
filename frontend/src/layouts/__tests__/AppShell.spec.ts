import { VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { computed, nextTick, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'

import AppShell from '@/layouts/AppShell.vue'

// Mock the session dependency (not the shell under test). data is a ref so the template
// unwraps it the same way Vue Query's would.
vi.mock('@/composables/useSession', () => ({
  useSession: () => ({
    data: ref({
      subject: 'alice',
      display_name: 'Alice',
      role: 'user',
      avatar: 'https://example.test/avatar.png',
    }),
  }),
}))

// The real banner is mounted (it is part of the chrome under test); only the verdict is faked.
const reachStatus = ref<'reachable' | 'unreachable'>('reachable')
vi.mock('@/composables/useReachability', () => ({
  useReachability: () => ({
    status: reachStatus,
    unreachable: computed(() => reachStatus.value === 'unreachable'),
    retryNow: () => {},
  }),
}))

const stub = { template: '<div />' }
const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: stub },
    { path: '/explore', component: stub },
    { path: '/places', component: stub },
    { path: '/archived', component: stub },
    { path: '/resources', component: stub },
    { path: '/settings', component: stub },
  ],
})

// TagList/CalendarHeatmap fetch via vue-query; stub them (covered by their own specs).
const Empty = { template: '<div />' }

async function mountShell(reachable = true) {
  reachStatus.value = reachable ? 'reachable' : 'unreachable'
  router.push('/')
  await router.isReady()
  return mount(AppShell, {
    global: {
      plugins: [router, VueQueryPlugin, PrimeVue],
      stubs: { TagList: Empty, CalendarHeatmap: Empty },
    },
  })
}

describe('AppShell', () => {
  it('renders a nav link for every primary section', async () => {
    const wrapper = await mountShell()
    for (const label of ['home', 'explore', 'places', 'archived', 'resources', 'settings']) {
      expect(wrapper.find(`[data-test="nav-${label}"]`).exists()).toBe(true)
    }
  })

  it('offers Places on the phone drawer too, not only the desktop sidebar', async () => {
    // The map is a phone feature as much as a desktop one (the in-view grid exists for it), and
    // the drawer is the only navigation a phone has.
    const wrapper = await mountShell()
    await wrapper.find('[data-test="menu-toggle"]').trigger('click')
    await nextTick()

    const drawer = document.querySelector('[data-test="mobile-drawer"]')
    expect(drawer?.querySelector('a[href="/places"]')?.textContent).toContain('Places')
  })

  it('shows the avatar (not the username text) and a dark-mode toggle', async () => {
    const wrapper = await mountShell()
    // The avatar alone identifies the signed-in user; the username text was removed as redundant.
    expect(wrapper.find('[data-test="header-avatar"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="user-name"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="dark-toggle"]').exists()).toBe(true)
  })

  it('has no in-app logout button (login is Authelia/OIDC SSO)', async () => {
    const wrapper = await mountShell()
    expect(wrapper.find('[data-test="logout"]').exists()).toBe(false)
  })

  it('pads the header with the iOS safe-area inset so the burger clears the status bar', async () => {
    const wrapper = await mountShell()
    // env(safe-area-inset-top) is 0 in normal browsers, non-zero only in iOS standalone.
    // Without it the header draws under the notch (the reported bug).
    expect(wrapper.find('header').classes()).toContain('pt-[env(safe-area-inset-top)]')
  })

  it('sticks the connection banner and the header together, not the header alone', async () => {
    // The banner has to survive a scroll: this app is normally read deep into the timeline, and
    // a banner left in the layout flow would be far off-screen when the connection drops.
    const wrapper = await mountShell(false)
    const chrome = wrapper.find('[data-test="app-chrome"]')
    expect(chrome.classes()).toEqual(expect.arrayContaining(['sticky', 'top-0', 'z-30']))
    expect(chrome.find('[data-test="connection-banner"]').exists()).toBe(true)
    expect(chrome.find('header').exists()).toBe(true)
    // The header must no longer stick on its own, or the two rows would overlap at the top.
    expect(wrapper.find('header').classes()).not.toContain('sticky')
  })

  it('keeps the chrome background and blur on the wrapper, so both rows read in either theme', async () => {
    const chrome = (await mountShell(false)).find('[data-test="app-chrome"]')
    expect(chrome.classes()).toEqual(
      expect.arrayContaining(['bg-surface-50/90', 'dark:bg-surface-950/90', 'backdrop-blur-sm']),
    )
  })

  it('pays the top safe-area inset exactly once — the banner owns it while it shows', async () => {
    const wrapper = await mountShell(false)
    // Both rows padding it would double the gap under the notch the moment the banner appears.
    expect(wrapper.find('header').classes()).not.toContain('pt-[env(safe-area-inset-top)]')
    expect(wrapper.find('header').classes()).toContain('pt-0')
    expect(wrapper.find('[data-test="connection-banner"]').classes()).toContain(
      'pt-[calc(0.5rem+env(safe-area-inset-top))]',
    )
  })

  it('hands the inset back to the header once the recovery notice has gone', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = await mountShell(false)
      reachStatus.value = 'reachable'
      await nextTick()
      // "Back online" is a row of chrome too, so it still owns the inset while it shows.
      expect(wrapper.find('[data-test="connection-recovered"]').exists()).toBe(true)
      expect(wrapper.find('header').classes()).toContain('pt-0')

      vi.advanceTimersByTime(3_000)
      await nextTick()
      await nextTick()
      expect(wrapper.find('[data-test="connection-recovered"]').exists()).toBe(false)
      expect(wrapper.find('header').classes()).toContain('pt-[env(safe-area-inset-top)]')
    } finally {
      vi.useRealTimers()
    }
  })
})
