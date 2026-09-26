import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { bulkDelete, deleteSummary, useBulkDelete } from '@/composables/useBulkDelete'

// The REAL generated client over a scripted transport, so the method, the path and the status the
// batch reacts to are all on the wire.
const { transport, toastAdd } = vi.hoisted(() => ({
  transport: vi.fn<(req: Request) => Promise<Response>>(),
  toastAdd: vi.fn(),
}))
vi.mock('@/composables/useApi', async () => {
  const { default: createClient } = await import('openapi-fetch')
  const client = createClient({ baseUrl: '/', fetch: (req: Request) => transport(req) })
  return { useApi: () => client }
})
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))

const noContent = () => new Response(null, { status: 204 })
const notFound = () =>
  new Response(JSON.stringify({ detail: 'Not found' }), {
    status: 404,
    headers: { 'Content-Type': 'application/json' },
  })
// A 404 with no body: openapi-fetch hands back `error: ''`, which is falsy — the status decides.
const bareNotFound = () => new Response(null, { status: 404 })

const sentPaths = () =>
  transport.mock.calls.map(([req]) => `${req.method} ${new URL(req.url).pathname}`)

function mountHost() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  let api!: ReturnType<typeof useBulkDelete>
  mount(
    defineComponent({
      setup() {
        api = useBulkDelete()
        return () => null
      },
    }),
    { global: { plugins: [[VueQueryPlugin, { queryClient }]] } },
  )
  return { api, invalidate }
}
const invalidatedKeys = (spy: ReturnType<typeof mountHost>['invalidate']) =>
  spy.mock.calls.map(([filters]) => (filters as { queryKey: unknown }).queryKey)

describe('bulkDelete', () => {
  beforeEach(() => {
    transport.mockReset()
    toastAdd.mockReset()
  })

  it('deletes one at a time, in order, through DELETE /api/attachments/{uid}', async () => {
    transport.mockImplementation(async () => noContent())
    const { useApi } = await import('@/composables/useApi')
    const result = await bulkDelete(useApi(), ['a', 'b', 'c'])
    expect(sentPaths()).toEqual([
      'DELETE /api/attachments/a',
      'DELETE /api/attachments/b',
      'DELETE /api/attachments/c',
    ])
    expect(result).toEqual({ done: ['a', 'b', 'c'], failed: null, unattempted: [] })
  })

  it('never has two deletes in flight', async () => {
    let inFlight = 0
    let peak = 0
    transport.mockImplementation(async () => {
      peak = Math.max(peak, ++inFlight)
      await new Promise((r) => setTimeout(r, 1))
      inFlight--
      return noContent()
    })
    const { useApi } = await import('@/composables/useApi')
    await bulkDelete(useApi(), ['a', 'b', 'c'])
    expect(peak).toBe(1)
  })

  it('stops on a 404: [a, b, c] answering [204, 404] is 1 done, 1 failed, c never sent', async () => {
    transport.mockResolvedValueOnce(noContent()).mockResolvedValueOnce(notFound())
    const { useApi } = await import('@/composables/useApi')
    const result = await bulkDelete(useApi(), ['a', 'b', 'c'])
    expect(result).toEqual({ done: ['a'], failed: 'b', unattempted: ['c'] })
    expect(sentPaths()).toEqual(['DELETE /api/attachments/a', 'DELETE /api/attachments/b'])
  })

  it('stops on a 404 whose body is empty', async () => {
    transport.mockResolvedValueOnce(bareNotFound())
    const { useApi } = await import('@/composables/useApi')
    const result = await bulkDelete(useApi(), ['a', 'b'])
    expect(result).toEqual({ done: [], failed: 'a', unattempted: ['b'] })
    expect(transport).toHaveBeenCalledTimes(1)
  })

  it('stops the same way on a transport exception', async () => {
    transport.mockResolvedValueOnce(noContent()).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { useApi } = await import('@/composables/useApi')
    const result = await bulkDelete(useApi(), ['a', 'b', 'c'])
    expect(result).toEqual({ done: ['a'], failed: 'b', unattempted: ['c'] })
    expect(transport).toHaveBeenCalledTimes(2)
  })

  it.each([
    [{ done: ['a', 'b'], failed: null, unattempted: [] }, 'Deleted 2'],
    [{ done: ['a'], failed: 'b', unattempted: ['c'] }, 'Deleted 1, 1 failed'],
    [{ done: [], failed: 'a', unattempted: [] }, 'Deleted 0, 1 failed'],
  ])('summarises %j as "%s"', (result, text) => {
    expect(deleteSummary(result)).toBe(text)
  })
})

describe('useBulkDelete', () => {
  beforeEach(() => {
    transport.mockReset()
    toastAdd.mockReset()
  })

  it('a failing second item stops, toasts 1 done / 1 failed and still invalidates the library and the feed', async () => {
    transport.mockResolvedValueOnce(noContent()).mockResolvedValueOnce(notFound())
    const { api, invalidate } = mountHost()
    const result = await api.run(['a', 'b', 'c'])
    expect(result).toEqual({ done: ['a'], failed: 'b', unattempted: ['c'] })
    expect(toastAdd).toHaveBeenCalledTimes(1)
    expect(toastAdd.mock.calls[0]![0]).toMatchObject({ severity: 'error', summary: 'Deleted 1, 1 failed' })
    expect(invalidatedKeys(invalidate)).toEqual([['attachments'], ['memos']])
  })

  it('a clean run toasts "Deleted N" and invalidates the same two prefixes', async () => {
    transport.mockImplementation(async () => noContent())
    const { api, invalidate } = mountHost()
    await api.run(['a', 'b'])
    expect(toastAdd.mock.calls[0]![0]).toMatchObject({ severity: 'success', summary: 'Deleted 2' })
    expect(invalidatedKeys(invalidate)).toEqual([['attachments'], ['memos']])
  })

  it('refuses a second run while one is in flight: one batch on the wire, not two', async () => {
    let release!: () => void
    transport.mockImplementationOnce(
      () => new Promise<Response>((r) => (release = () => r(noContent()))),
    )
    const { api } = mountHost()
    const first = api.run(['a'])
    expect(api.pending.value).toBe(true)
    const second = await api.run(['a'])
    expect(second).toBeNull()
    release()
    await first
    expect(transport).toHaveBeenCalledTimes(1)
    expect(api.pending.value).toBe(false)
  })

  it('an empty batch sends nothing and toasts nothing', async () => {
    const { api, invalidate } = mountHost()
    expect(await api.run([])).toBeNull()
    expect(transport).not.toHaveBeenCalled()
    expect(toastAdd).not.toHaveBeenCalled()
    expect(invalidate).not.toHaveBeenCalled()
  })
})
