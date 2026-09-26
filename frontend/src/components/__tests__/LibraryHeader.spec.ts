import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import LibraryHeader from '@/components/LibraryHeader.vue'
import { fromSearchParams, type LibraryFilter } from '@/lib/libraryFilter'

// The real tags query through the real generated client; only the transport is scripted.
const { transport } = vi.hoisted(() => ({
  transport: vi.fn<(req: Request) => Promise<Response>>(),
}))
vi.mock('@/composables/useApi', async () => {
  const { default: createClient } = await import('openapi-fetch')
  const client = createClient({ baseUrl: '/', fetch: (req: Request) => transport(req) })
  return { useApi: () => client }
})

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function mountHeader(query: string) {
  const filter = fromSearchParams(new URLSearchParams(query))
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(LibraryHeader, {
    attachTo: document.body,
    props: { filter },
    global: { plugins: [[VueQueryPlugin, { queryClient }], PrimeVue] },
  })
}

async function openMenu(wrapper: ReturnType<typeof mountHeader>) {
  await wrapper.find('[data-test="library-options"]').trigger('click')
  for (let i = 0; i < 5; i++) await flushPromises()
}

// The menu is a PrimeVue popup teleported to body.
const row = (id: string) => document.body.querySelector<HTMLElement>(`[data-test="menu-${id}"]`)
const checked = (id: string) => row(id)?.getAttribute('data-checked')

describe('LibraryHeader', () => {
  beforeEach(() => transport.mockReset())
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('shows the title, the round glass options button and the inert glass Select capsule', () => {
    transport.mockResolvedValue(json([]))
    const wrapper = mountHeader('')
    expect(wrapper.find('h1').text()).toBe('Library')
    const options = wrapper.find('[data-test="library-options"]')
    expect(options.classes()).toEqual(expect.arrayContaining(['glass', 'rounded-full']))
    const select = wrapper.find('[data-test="library-select"]')
    expect(select.text()).toBe('Select')
    expect(select.classes()).toContain('glass')
    expect(select.attributes('aria-disabled')).toBe('true')
    // The tags are asked for when the menu opens, not on every visit.
    expect(transport).not.toHaveBeenCalled()
  })

  it('says the current view, order and tag under the title', () => {
    expect(mountHeader('').find('[data-test="library-subtitle"]').text()).toBe(
      'All Items · Newest First',
    )
    expect(
      mountHeader('type=video&order=oldest&tag=recap').find('[data-test="library-subtitle"]').text(),
    ).toBe('Videos · Oldest First · #recap')
  })

  it('opens the menu with the sort, kind and tag rows checked per the route query', async () => {
    transport.mockResolvedValue(
      json([
        { name: 'holiday', count: 2 },
        { name: 'recap', count: 4 },
      ]),
    )
    const wrapper = mountHeader('type=video&order=oldest&tag=recap')
    await openMenu(wrapper)

    expect(checked('order-oldest')).toBe('true')
    expect(checked('order-newest')).toBe('false')
    expect(checked('kind-video')).toBe('true')
    expect(checked('kind-all')).toBe('false')
    expect(checked('kind-image')).toBe('false')
    expect(checked('tag-recap')).toBe('true')
    expect(checked('tag-all')).toBe('false')
    expect(checked('tag-holiday')).toBe('false')
    // The picture counts come from the tags endpoint, counted for the active kind.
    expect(row('tag-recap')?.querySelector('[data-test="tag-count"]')?.textContent).toBe('4')
    expect(row('tag-holiday')?.querySelector('[data-test="tag-count"]')?.textContent).toBe('2')
    const sent = new URL(transport.mock.calls[0]![0].url)
    expect(sent.pathname).toBe('/api/attachments/tags')
    expect(sent.searchParams.get('type')).toBe('video')
    // No "Mine" (deferred) and nothing that searches.
    expect(document.body.innerHTML).not.toMatch(/\bmine\b/i)
    expect(row('order-oldest')?.textContent).toContain('Sort by Oldest First')
  })

  it('checks the defaults on a bare route', async () => {
    transport.mockResolvedValue(json([{ name: 'recap', count: 4 }]))
    const wrapper = mountHeader('')
    await openMenu(wrapper)
    expect(checked('order-newest')).toBe('true')
    expect(checked('kind-all')).toBe('true')
    expect(checked('tag-all')).toBe('true')
    expect(new URL(transport.mock.calls[0]![0].url).searchParams.has('type')).toBe(false)
  })

  it.each<[string, LibraryFilter]>([
    ['order-newest', { type: 'video', order: 'newest', tag: 'recap' }],
    ['kind-all', { order: 'oldest', tag: 'recap' }],
    ['kind-image', { type: 'image', order: 'oldest', tag: 'recap' }],
    ['tag-all', { type: 'video', order: 'oldest' }],
    ['tag-holiday', { type: 'video', order: 'oldest', tag: 'holiday' }],
  ])('hands back the filter with %s picked, the other groups untouched', async (id, expected) => {
    transport.mockResolvedValue(json([{ name: 'holiday', count: 2 }]))
    const wrapper = mountHeader('type=video&order=oldest&tag=recap')
    await openMenu(wrapper)
    row(id)!.click()
    await flushPromises()
    expect(wrapper.emitted('update:filter')).toEqual([[expected]])
  })

  it('keeps an active tag the counts do not name, checked, so the menu can clear it', async () => {
    transport.mockResolvedValue(json([{ name: 'holiday', count: 2 }]))
    const wrapper = mountHeader('tag=gone')
    await openMenu(wrapper)
    expect(checked('tag-gone')).toBe('true')
  })

  it('says the tags could not be loaded rather than offering none', async () => {
    transport.mockResolvedValue(json({ detail: 'no' }, 500))
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 200 })))
    try {
      const wrapper = mountHeader('')
      await openMenu(wrapper)
      expect(row('tags-error')).not.toBeNull()
      expect(checked('tag-all')).toBe('true')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
