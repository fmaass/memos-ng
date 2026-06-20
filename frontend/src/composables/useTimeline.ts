import { useInfiniteQuery } from '@tanstack/vue-query'
import { computed, type MaybeRefOrGetter, toValue } from 'vue'

import { useApi } from '@/composables/useApi'

export type MemoQuery = Record<string, string | boolean | string[]>

// Cursor-paginated memo feed, reused by Home (no query), Explore, Archived and Search by
// passing a filter `query`. The query may be reactive (a ref/getter) so Search re-runs as
// the term changes. Home's empty-query feed keeps the ['memos','timeline'] key so the SSE
// patcher (useMediaEvents) targets it.
export function useTimeline(query: MaybeRefOrGetter<MemoQuery> = {}) {
  const api = useApi()
  const resolved = computed(() => toValue(query))
  const queryKey = computed(() => {
    const q = resolved.value
    return ['memos', Object.keys(q).length ? q : 'timeline'] as const
  })

  return useInfiniteQuery({
    queryKey,
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      const params = { ...resolved.value, ...(pageParam ? { cursor: pageParam } : {}) }
      const { data, error } = await api.GET('/api/memos', { params: { query: params } })
      if (error) throw error
      return data
    },
    getNextPageParam: (lastPage) => lastPage.nextCursor ?? undefined,
  })
}
