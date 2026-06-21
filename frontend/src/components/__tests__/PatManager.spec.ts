import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PatManager from '@/components/PatManager.vue'

const { getPats, postPat, deletePat } = vi.hoisted(() => ({
  getPats: vi.fn(),
  postPat: vi.fn(),
  deletePat: vi.fn(),
}))
vi.mock('@/composables/useApi', () => ({
  useApi: () => ({ GET: getPats, POST: postPat, DELETE: deletePat }),
}))

const invalidateQueries = vi.fn()
const patsRef = ref<unknown[]>([])
vi.mock('@tanstack/vue-query', () => ({
  useQueryClient: () => ({ invalidateQueries }),
  useQuery: () => ({ data: patsRef }),
  // run mutations inline so the test exercises mutationFn + onSuccess(result)
  useMutation: (opts: {
    mutationFn: (v: never) => Promise<unknown>
    onSuccess?: (r: unknown) => void
  }) => ({
    isPending: ref(false),
    mutate: async (v: never) => {
      const result = await opts.mutationFn(v)
      opts.onSuccess?.(result)
    },
  }),
}))

function mountPats() {
  return mount(PatManager, { global: { plugins: [PrimeVue, ConfirmationService, ToastService] } })
}

describe('PatManager', () => {
  beforeEach(() => {
    patsRef.value = []
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    })
  })
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('creates a token and shows the plaintext exactly once, with copy', async () => {
    postPat.mockResolvedValue({
      data: { id: 1, name: 'ci', token: 'memos_pat_secret', last_used: null, created_at: 'x' },
      error: undefined,
    })
    const wrapper = mountPats()
    await wrapper.find('[data-test="pat-new"]').trigger('click')
    await wrapper.find('[data-test="pat-name"]').setValue('ci')
    await wrapper.find('[data-test="pat-create"]').trigger('click')
    await flushPromises()

    expect(postPat).toHaveBeenCalledWith('/api/pats', { body: { name: 'ci' } })
    expect(wrapper.find('[data-test="pat-token"]').text()).toBe('memos_pat_secret')
    expect(invalidateQueries).toHaveBeenCalled()

    await wrapper.find('[data-test="pat-copy"]').trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('memos_pat_secret')
  })

  it('lists existing tokens and revokes one via DELETE', async () => {
    patsRef.value = [{ id: 7, name: 'old', last_used: null, created_at: 'x' }]
    deletePat.mockResolvedValue({ error: undefined })
    const wrapper = mountPats()
    expect(wrapper.findAll('[data-test="pat-row"]')).toHaveLength(1)

    await wrapper.find('[data-test="pat-revoke"]').trigger('click')
    await flushPromises()
    expect(deletePat).toHaveBeenCalledWith('/api/pats/{pat_id}', { params: { path: { pat_id: 7 } } })
    expect(invalidateQueries).toHaveBeenCalled()
  })
})
