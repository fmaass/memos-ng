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
  useSession: () => ({ data: ref({ subject: 'alice', display_name: 'Alice', role: 'user' }) }),
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

  it('shows the authenticated user and a dark-mode toggle', async () => {
    const wrapper = await mountShell()
    expect(wrapper.find('[data-test="user-name"]').text()).toBe('Alice')
    expect(wrapper.find('[data-test="dark-toggle"]').exists()).toBe(true)
  })
})
