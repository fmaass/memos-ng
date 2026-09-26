import { VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { computed, defineComponent, h, markRaw, nextTick, ref } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'

import { useDockSegment } from '@/composables/useDockSegment'
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
// A routed view that registers a dock segment the way the Library does — mounted on `/` as well,
// so "never on `/`" is tested with a segment actually registered there.
const Pill = markRaw(defineComponent({ render: () => h('div', { 'data-test': 'registered-pill' }) }))
const RegisteringView = defineComponent({
  setup() {
    useDockSegment(Pill)
    return () => h('div', { 'data-test': 'registering-view' })
  },
})
const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: RegisteringView },
    { path: '/explore', component: stub },
    { path: '/places', component: stub },
    { path: '/archived', component: stub },
    { path: '/resources', component: RegisteringView },
    { path: '/search', component: stub },
    { path: '/settings', component: stub },
  ],
})

// TagList/CalendarHeatmap fetch via vue-query; stub them (covered by their own specs).
const Empty = { template: '<div />' }

async function mountShell(reachable = true, path = '/', routed = false) {
  reachStatus.value = reachable ? 'reachable' : 'unreachable'
  await router.push(path)
  await router.isReady()
  return mount(AppShell, {
    attachTo: routed ? document.body : undefined,
    slots: routed ? { default: () => h(RouterView) } : {},
    global: {
      plugins: [router, VueQueryPlugin, PrimeVue],
      stubs: { TagList: Empty, CalendarHeatmap: Empty },
    },
  })
}

describe('AppShell', () => {
  it('renders a nav link for every secondary section; the three main ones moved to the dock', async () => {
    const wrapper = await mountShell()
    for (const label of ['explore', 'text-search', 'archived', 'settings']) {
      expect(wrapper.find(`[data-test="nav-${label}"]`).exists()).toBe(true)
    }
    for (const label of ['home', 'places', 'resources']) {
      expect(wrapper.find(`[data-test="nav-${label}"]`).exists()).toBe(false)
    }
    for (const tab of ['library', 'timeline', 'places']) {
      expect(wrapper.find(`[data-test="dock-tab-${tab}"]`).exists()).toBe(true)
    }
  })

  it('offers Search on the phone drawer, and Places on the dock a phone always shows', async () => {
    // Text search is reached only from the drawer now (no header carries a search button), and the
    // map moved from the drawer to the dock's tabs.
    const wrapper = await mountShell(true, '/explore')
    await wrapper.find('[data-test="menu-toggle"]').trigger('click')
    await nextTick()

    const drawer = document.querySelector('[data-test="mobile-drawer"]')
    expect(drawer?.querySelector('a[href="/search"]')?.textContent).toContain('Search')
    expect(wrapper.find('[data-test="dock-tab-places"]').attributes('href')).toBe('/places')
  })

  it.each(['/', '/resources', '/explore'])('carries no header search link on %s', async (path) => {
    const wrapper = await mountShell(true, path)
    expect(wrapper.find('[data-test="nav-search"]').exists()).toBe(false)
    expect(wrapper.find('header').html()).not.toMatch(/search/i)
    expect(wrapper.find('[data-test="dark-toggle"]').exists()).toBe(true) // control: header rendered
  })

  it('gives `/` the Timeline header instead of the drawer button', async () => {
    const home = await mountShell(true, '/')
    expect(home.find('[data-test="menu-toggle"]').exists()).toBe(false)
    expect(home.find('[data-test="timeline-settings"]').exists()).toBe(true)
    expect(home.find('[data-test="timeline-new-memo"]').exists()).toBe(true)

    const other = await mountShell(true, '/resources')
    expect(other.find('[data-test="menu-toggle"]').exists()).toBe(true)
    expect(other.find('[data-test="timeline-new-memo"]').exists()).toBe(false)
  })

  it('renders the routed view’s registered segment in the dock on /resources only, in segment mode', async () => {
    // The real hierarchy: shell → RouterView → a view that registers a segment. provide/inject can
    // only flow shell → view, so this is the path the Library's pill takes to the dock.
    const onLibrary = await mountShell(true, '/resources', true)
    expect(onLibrary.find('[data-test="registering-view"]').exists()).toBe(true)
    expect(onLibrary.find('[data-test="registered-pill"]').exists()).toBe(false) // tabs at rest
    await onLibrary.find('[data-test="dock-tab-library"]').trigger('click')
    await nextTick()
    expect(onLibrary.find('[data-test="registered-pill"]').exists()).toBe(true)
    onLibrary.unmount()

    const onTimeline = await mountShell(true, '/', true)
    expect(onTimeline.find('[data-test="registering-view"]').exists()).toBe(true)
    await onTimeline.find('[data-test="dock-tab-timeline"]').trigger('click')
    document.scrollingElement!.scrollTop = 5000
    window.dispatchEvent(new Event('scroll'))
    await nextTick()
    expect(onTimeline.find('[data-test="registered-pill"]').exists()).toBe(false)
    document.scrollingElement!.scrollTop = 0
    onTimeline.unmount()
  })

  it('keeps the last row clear of the dock where the document scrolls, and not on /places', async () => {
    const library = await mountShell(true, '/resources')
    expect(library.find('main').classes()).toContain('pb-[var(--dock-clearance)]')
    // Places scrolls an inner region of a viewport-high view; the view pads that region instead.
    const places = await mountShell(true, '/places')
    expect(places.find('main').classes()).not.toContain('pb-[var(--dock-clearance)]')
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
