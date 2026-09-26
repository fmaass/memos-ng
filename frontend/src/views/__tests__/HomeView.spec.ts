import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TIMELINE_COMPOSER_KEY } from '@/composables/useTimelineComposer'
import HomeView from '@/views/HomeView.vue'

// CaptureEditor (rendered by Home) depends on the API; mock it (not the view under test).
vi.mock('@/composables/useApi', () => ({
  useApi: () => ({ GET: vi.fn(), POST: vi.fn() }),
}))
// HomeView invalidates the feed on @saved via the query client; stub it.
vi.mock('@tanstack/vue-query', () => ({ useQueryClient: () => ({ invalidateQueries: vi.fn() }) }))
// HomeView reads the route's ?tag; provide a stub route.
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))
// CaptureEditor (child) uses settings via vue-query; stub it out.
vi.mock('@/composables/useSettings', async () => {
  const { ref } = await import('vue')
  return { useSettings: () => ({ settings: ref(undefined), update: { mutate: () => {} } }) }
})

const FeedStub = { template: '<div data-test="feed-stub" />' }

function mountHome(composerOpen = false) {
  return mount(HomeView, {
    global: {
      plugins: [PrimeVue, ConfirmationService, ToastService],
      stubs: { MemoFeed: FeedStub },
      provide: { [TIMELINE_COMPOSER_KEY as symbol]: ref(composerOpen) },
    },
  })
}
function phone(isPhone: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: isPhone, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  )
}

describe('HomeView', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('renders no inline editor at rest: the feed alone, the editor waits behind new memo', () => {
    const wrapper = mountHome(false)
    expect(wrapper.find('[data-test="capture-editor"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="feed-stub"]').exists()).toBe(true)
  })

  it('composes the capture editor above the memo feed once new memo is pressed on md+', () => {
    phone(false)
    const wrapper = mountHome(true)
    const editor = wrapper.find('[data-test="capture-editor"]')
    expect(editor.exists()).toBe(true)
    // Above the feed: the editor precedes it in document order.
    const feed = wrapper.find('[data-test="feed-stub"]').element
    expect(editor.element.compareDocumentPosition(feed) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('leaves the editor to the header dialog on a phone', () => {
    phone(true)
    expect(mountHome(true).find('[data-test="capture-editor"]').exists()).toBe(false)
  })

  it('reads in the shared content column (wider on xl desktops)', () => {
    const wrapper = mountHome()
    const classes = wrapper.find('[data-test="view-home"]').classes()
    expect(classes).toContain('max-w-2xl')
    expect(classes).toContain('xl:max-w-4xl')
  })
})
