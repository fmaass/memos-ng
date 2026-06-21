import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { applyMediaEvent, useMediaEvents } from '@/composables/useMediaEvents'

const setQueriesData = vi.fn()
vi.mock('@tanstack/vue-query', () => ({ useQueryClient: () => ({ setQueriesData }) }))

describe('applyMediaEvent', () => {
  it('updates only the matching attachment status across pages', () => {
    const data = {
      pages: [
        { items: [{ attachments: [{ uid: 'x', status: 'processing' }, { uid: 'y', status: 'ready' }] }] },
      ],
    }
    const next = applyMediaEvent(data, { uid: 'x', status: 'ready' })
    const atts = next.pages[0].items[0].attachments!
    expect(atts.find((a) => a.uid === 'x')!.status).toBe('ready')
    expect(atts.find((a) => a.uid === 'y')!.status).toBe('ready') // unchanged
    expect(data.pages[0].items[0].attachments![0].status).toBe('processing') // original untouched
  })
})

describe('useMediaEvents', () => {
  let instance: { url: string; onmessage?: (e: { data: string }) => void; close: () => void } | null =
    null
  afterEach(() => {
    vi.unstubAllGlobals()
    setQueriesData.mockClear()
  })

  it('opens the SSE channel and patches the timeline cache on a message', () => {
    vi.stubGlobal(
      'EventSource',
      class {
        url: string
        onmessage?: (e: { data: string }) => void
        constructor(url: string) {
          this.url = url
          instance = this as unknown as typeof instance
        }
        close() {}
      },
    )
    const Comp = defineComponent({
      setup() {
        useMediaEvents()
        return () => h('div')
      },
    })
    mount(Comp)
    expect(instance!.url).toBe('/api/events')

    instance!.onmessage!({ data: JSON.stringify({ uid: 'x', status: 'ready' }) })
    expect(setQueriesData).toHaveBeenCalledWith({ queryKey: ['memos'] }, expect.any(Function))
  })
})
