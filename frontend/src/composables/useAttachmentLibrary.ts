import { useInfiniteQuery } from '@tanstack/vue-query'
import { computed, type MaybeRefOrGetter, toValue } from 'vue'

import type { components } from '@/api/schema'
import { useApi } from '@/composables/useApi'
import { appendPage, nextCursorFrom } from '@/lib/attachmentPaging'

type AttachmentListItem = components['schemas']['AttachmentListItem']

export type LibraryFilter = 'all' | 'image' | 'video'

// The server's own default page size (app/routers/assets.py `limit: int = 200`), sent explicitly
// because the cursor is only valid with the SAME `type`, `limit` and `order` it was minted for.
export const LIBRARY_PAGE_SIZE = 200

interface LibraryPage {
  items: AttachmentListItem[]
  nextCursor: string | undefined
}

// The whole media library, page by page. The list body is a bare array, so the continuation lives
// in the X-Next-Cursor response header; openapi-fetch hands back the raw Response and both client
// middlewares pass it through, so the header is read here. The key stays ['attachments', filter]:
// every invalidation of ['attachments'] (delete, print-master attach/remove) still reaches it.
export function useAttachmentLibrary(filter: MaybeRefOrGetter<LibraryFilter>) {
  const api = useApi()
  const query = useInfiniteQuery({
    queryKey: computed(() => ['attachments', toValue(filter)] as const),
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ queryKey, pageParam }): Promise<LibraryPage> => {
      const type = queryKey[1]
      const { data, error, response } = await api.GET('/api/attachments', {
        params: {
          query: {
            limit: LIBRARY_PAGE_SIZE,
            ...(type !== 'all' ? { type } : {}),
            ...(pageParam ? { cursor: pageParam } : {}),
          },
        },
      })
      if (error) throw error
      return { items: data, nextCursor: nextCursorFrom(response.headers) }
    },
    getNextPageParam: (lastPage) => lastPage.nextCursor,
  })
  const items = computed(() => appendPage(query.data.value?.pages.map((p) => p.items) ?? []))
  return { ...query, items }
}
