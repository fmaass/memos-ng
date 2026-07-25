import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import ArchivedView from '@/views/ArchivedView.vue'

const patch = vi.fn().mockResolvedValue({ error: undefined })
vi.mock('@/composables/useApi', () => ({ useApi: () => ({ PATCH: patch }) }))

const invalidateQueries = vi.fn()
vi.mock('@tanstack/vue-query', () => ({
  useQueryClient: () => ({ invalidateQueries }),
  // run the mutation inline so the test exercises mutationFn + onSuccess
  useMutation: (opts: {
    mutationFn: (v: string) => Promise<unknown>
    onSuccess?: () => void
  }) => ({
    mutate: async (v: string) => {
      await opts.mutationFn(v)
      opts.onSuccess?.()
    },
  }),
}))

const FeedStub = {
  props: ['query', 'restorable'],
  emits: ['restore'],
  template: '<button data-test="do-restore" @click="$emit(\'restore\', \'u9\')">restore</button>',
}

describe('ArchivedView', () => {
  it('restores an archived memo via PATCH archived:false and refreshes', async () => {
    const wrapper = mount(ArchivedView, { global: { stubs: { MemoFeed: FeedStub } } })
    await wrapper.find('[data-test="do-restore"]').trigger('click')
    await flushPromises()
    expect(patch).toHaveBeenCalledWith('/api/memos/{uid}', {
      params: { path: { uid: 'u9' } },
      body: { archived: false },
    })
    expect(invalidateQueries).toHaveBeenCalled()
  })

  it('reads in the shared content column (wider on xl desktops)', () => {
    const wrapper = mount(ArchivedView, { global: { stubs: { MemoFeed: FeedStub } } })
    const classes = wrapper.find('[data-test="view-archived"]').classes()
    expect(classes).toContain('max-w-2xl')
    expect(classes).toContain('xl:max-w-4xl')
  })
})
