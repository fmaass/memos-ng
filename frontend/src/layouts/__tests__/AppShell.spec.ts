import { VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { ref } from 'vue'
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

const stub = { template: '<div />' }
const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: stub },
    { path: '/explore', component: stub },
    { path: '/archived', component: stub },
    { path: '/resources', component: stub },
    { path: '/settings', component: stub },
  ],
})

// TagList/CalendarHeatmap fetch via vue-query; stub them (covered by their own specs).
const Empty = { template: '<div />' }

async function mountShell() {
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
    for (const label of ['home', 'explore', 'archived', 'resources', 'settings']) {
      expect(wrapper.find(`[data-test="nav-${label}"]`).exists()).toBe(true)
    }
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
    // env(safe-area-inset-top) is 0 in normal browsers, non-zero only in iOS standalone — applied
    // unconditionally. Without it the header draws under the notch (the reported bug).
    expect(wrapper.find('header').classes()).toContain('pt-[env(safe-area-inset-top)]')
  })
})
