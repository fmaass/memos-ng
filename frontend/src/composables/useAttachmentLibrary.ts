import { useInfiniteQuery, useQuery } from '@tanstack/vue-query'
import { computed, type MaybeRefOrGetter, toValue } from 'vue'

import type { components } from '@/api/schema'
import { useApi } from '@/composables/useApi'
import { appendPage, nextCursorFrom } from '@/lib/attachmentPaging'
import {
  type BucketSize,
  type LibraryFilter,
  type LibraryKind,
  toApiParams,
  toBucketParams,
} from '@/lib/libraryFilter'

type AttachmentListItem = components['schemas']['AttachmentListItem']

// The server's maximum page (app/routers/assets.py allows up to 500), so the jump from a Months
// day into All walks as few pages as it can. Sent explicitly with every page because the cursor is
// only valid with the SAME `type`, `limit`, `order` and `tag` it was minted for — which is why the
// jump advances THIS query rather than walking a second list beside it.
export const LIBRARY_PAGE_SIZE = 500

interface LibraryPage {
  items: AttachmentListItem[]
  nextCursor: string | undefined
}

// The whole media library, page by page. The list body is a bare array, so the continuation lives
// in the X-Next-Cursor response header; openapi-fetch hands back the raw Response and both client
// middlewares pass it through, so the header is read here. The key is ['attachments', filter] with
// the WHOLE filter inside, so a tag or order switch can never be served another filter's pages or
// continue with its cursor, and every invalidation of ['attachments'] (delete, print-master
// attach/remove) still reaches it.
export function useAttachmentLibrary(filter: MaybeRefOrGetter<LibraryFilter>) {
  const api = useApi()
  const query = useInfiniteQuery({
    queryKey: computed(() => ['attachments', { ...toValue(filter) }] as const),
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ queryKey, pageParam }): Promise<LibraryPage> => {
      const { data, error, response } = await api.GET('/api/attachments', {
        params: {
          query: {
            limit: LIBRARY_PAGE_SIZE,
            ...toApiParams(queryKey[1]),
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

// Every tag with its PICTURE count for the options menu (the memo tag counts from useTags count
// memos, not pictures). Counted per kind, so the kind is in the key; under the ['attachments']
// prefix, so the invalidation a delete sends refreshes the counts too. `enabled` keeps it from
// asking before anyone opens the menu.
export function useAttachmentTags(
  type: MaybeRefOrGetter<LibraryKind | undefined>,
  enabled: MaybeRefOrGetter<boolean> = true,
) {
  const api = useApi()
  return useQuery({
    queryKey: computed(() => ['attachments', 'tags', toValue(type) ?? null] as const),
    queryFn: async ({ queryKey }) => {
      const kind = queryKey[2]
      const { data, error } = await api.GET('/api/attachments/tags', {
        params: { query: kind ? { type: kind } : {} },
      })
      if (error) throw error
      return data
    },
    enabled: computed(() => toValue(enabled)),
  })
}

// Counts and covers per Zurich calendar day, month or year for the Years and Months views. The
// key is ['attachments', 'buckets', size, filter], under the ['attachments'] prefix, so a delete or a
// print-master change refreshes the counts along with the list. Its params are toBucketParams's —
// never the list's, which carry an order the endpoint does not take.
export function useAttachmentBuckets(
  size: MaybeRefOrGetter<BucketSize>,
  filter: MaybeRefOrGetter<LibraryFilter>,
  enabled: MaybeRefOrGetter<boolean> = true,
) {
  const api = useApi()
  return useQuery({
    queryKey: computed(() => ['attachments', 'buckets', toValue(size), { ...toValue(filter) }] as const),
    queryFn: async ({ queryKey }) => {
      const { data, error } = await api.GET('/api/attachments/buckets', {
        params: { query: toBucketParams(queryKey[2], queryKey[3]) },
      })
      if (error) throw error
      return data
    },
    enabled: computed(() => toValue(enabled)),
  })
}
