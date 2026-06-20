import { useInfiniteQuery } from '@tanstack/vue-query'

import { useApi } from '@/composables/useApi'

// Cursor-paginated memo timeline. Each page is a MemoList {items, nextCursor, totalCount};
// the next page is fetched with ?cursor=nextCursor until it is null.
export function useTimeline() {
  const api = useApi()
  return useInfiniteQuery({
    queryKey: ['memos', 'timeline'],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      const { data, error } = await api.GET('/api/memos', {
        params: { query: pageParam ? { cursor: pageParam } : {} },
      })
      if (error) throw error
      return data
    },
    getNextPageParam: (lastPage) => lastPage.nextCursor ?? undefined,
  })
}
