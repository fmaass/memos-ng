import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { defineComponent, h, nextTick, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import TimelineHeader from '@/components/TimelineHeader.vue'

// The editor's own dependencies (covered by CaptureEditor.spec); the header under test is real.
vi.mock('@/composables/useApi', () => ({ useApi: () => ({ GET: vi.fn(), POST: vi.fn() }) }))
vi.mock('@/composables/useSettings', async () => {
  const { ref } = await import('vue')
  return { useSettings: () => ({ settings: ref(undefined), update: { mutate: () => {} } }) }
})
const { invalidateQueries } = vi.hoisted(() => ({ invalidateQueries: vi.fn() }))
vi.mock('@tanstack/vue-query', () => ({ useQueryClient: () => ({ invalidateQueries }) }))

function phone(isPhone: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: isPhone, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  )
}

async function mountHeader() {
  const stub = { template: '<div />' }
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: stub },
      { path: '/settings', component: stub },
    ],
  })
  await router.push('/')
  await router.isReady()
  const open = ref(false)
  const Host = defineComponent({
    render: () =>
      h('header', [
        h(TimelineHeader, { open: open.value, 'onUpdate:open': (v: boolean) => (open.value = v) }),
      ]),
  })
  const wrapper = mount(Host, {
    attachTo: document.body,
    global: { plugins: [router, PrimeVue, ConfirmationService, ToastService] },
  })
  return { wrapper, router, open }
}

const editorShown = () => document.body.querySelector('[data-test="capture-editor"]') !== null

describe('TimelineHeader', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('renders exactly the two glass buttons: settings and new memo, and no search', async () => {
    phone(true)
    const { wrapper } = await mountHeader()
    expect(wrapper.find('[data-test="timeline-settings"]').classes()).toContain('glass')
    expect(wrapper.find('[data-test="timeline-new-memo"]').classes()).toContain('glass')
    expect(wrapper.findAll('a, button')).toHaveLength(2)
    expect(wrapper.html()).not.toMatch(/search/i)
  })

  it('routes the settings button to Settings', async () => {
    phone(true)
    const { wrapper, router } = await mountHeader()
    await wrapper.find('[data-test="timeline-settings"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/settings')
  })

  it('mounts the capture editor behind the new-memo button on a phone', async () => {
    phone(true)
    const { wrapper, open } = await mountHeader()
    expect(editorShown()).toBe(false)

    await wrapper.find('[data-test="timeline-new-memo"]').trigger('click')
    await nextTick()
    await flushPromises()

    expect(open.value).toBe(true)
    expect(editorShown()).toBe(true)
  })

  it('leaves the editor to the feed on md+ (it opens inline there, not in a dialog)', async () => {
    phone(false)
    const { wrapper, open } = await mountHeader()
    await wrapper.find('[data-test="timeline-new-memo"]').trigger('click')
    await flushPromises()
    expect(open.value).toBe(true)
    expect(editorShown()).toBe(false)
  })
})
