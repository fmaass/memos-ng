<script setup lang="ts">
import { useMutation, useQueryClient } from '@tanstack/vue-query'
import { computed, onMounted, onUnmounted, ref } from 'vue'

import MemoCard from '@/components/MemoCard.vue'
import MemoCardSkeleton from '@/components/MemoCardSkeleton.vue'
import { useApi } from '@/composables/useApi'
import { usePrintUpload } from '@/composables/usePrintUpload'
import { type MemoQuery, useTimeline } from '@/composables/useTimeline'
import { shouldRemint } from '@/lib/imageRetry'
import { groupByDay } from '@/lib/timeline'

// Generic cursor-paginated memo feed. Home passes no query; Explore/Search pass filters;
// Archived passes restorable to surface a per-card Restore action.
const props = withDefaults(defineProps<{ query?: MemoQuery; restorable?: boolean }>(), {
  query: () => ({}),
  restorable: false,
})
const emit = defineEmits<{ restore: [uid: string] }>()

const api = useApi()
const queryClient = useQueryClient()

const { data, fetchNextPage, hasNextPage, isFetchingNextPage, isFetching, isPending, isError, refetch } =
  useTimeline(() => props.query)

const memos = computed(() => data.value?.pages.flatMap((page) => page.items) ?? [])
const days = computed(() => groupByDay(memos.value))

// A tile bubbled `retry` after exhausting its in-place cache-bust retries — its signed URL has
// likely expired (the bucket rolled while the feed sat open). Cache-busting can't refresh a
// signature, so re-mint by refetching the timeline, which returns freshly-signed URLs. Gated so a
// burst (or steady stream) of failing tiles triggers one refetch per cooldown window, not a loop.
let lastRemint = 0
function onTileRetry() {
  if (!shouldRemint(Date.now(), lastRemint, isFetching.value)) return
  lastRemint = Date.now()
  void refetch()
}

// Persist a task-checkbox toggle (PATCH the memo content) and refresh the feed.
const toggleTask = useMutation({
  mutationFn: async ({ uid, content }: { uid: string; content: string }) => {
    const { error } = await api.PATCH('/api/memos/{uid}', {
      params: { path: { uid } },
      body: { content },
    })
    if (error) throw error
  },
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memos'] }),
})

// Add/remove an emoji reaction and refresh.
const toggleReaction = useMutation({
  mutationFn: async ({ uid, emoji, add }: { uid: string; emoji: string; add: boolean }) => {
    const { error } = add
      ? await api.POST('/api/memos/{uid}/reactions', { params: { path: { uid } }, body: { emoji } })
      : await api.DELETE('/api/memos/{uid}/reactions/{emoji}', {
          params: { path: { uid, emoji } },
        })
    if (error) throw error
  },
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memos'] }),
})

// Edit a memo's content / visibility / display date + attach any new images (owner-only on the
// server) and refresh.
const editMemo = useMutation({
  mutationFn: async (p: {
    uid: string
    content: string
    visibility: string
    created_at: string
    upload_uids: string[]
  }) => {
    const { error } = await api.PATCH('/api/memos/{uid}', {
      params: { path: { uid: p.uid } },
      body: {
        content: p.content,
        visibility: p.visibility as never,
        created_at: p.created_at,
        upload_uids: p.upload_uids,
      },
    })
    if (error) throw error
  },
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memos'] }),
})

// Attach a print-quality master to one of your image attachments and refresh. Shared with the
// Resources lightbox — the composable carries the success/error toasts (MEMNG-74).
const addPrint = usePrintUpload(['memos'])

// Delete one of a memo's attachments (owner-only on the server) and refresh the feed.
const deleteAttachment = useMutation({
  mutationFn: async (uid: string) => {
    const { error } = await api.DELETE('/api/attachments/{uid}', { params: { path: { uid } } })
    if (error) throw error
  },
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memos'] }),
})

// "Remove all" — delete the memo's attachments ONE AT A TIME. A parallel fan-out (one request per
// uid) would check out a pooled DB connection each and exhaust the bounded shared Postgres pool
// (5/5 — CARDINAL), 500-ing deletes and starving concurrent uploads. Sequential = ≤1 connection at a
// time. Refresh the feed once, at the end.
const deleteAttachments = useMutation({
  mutationFn: async (uids: string[]) => {
    for (const uid of uids) {
      const { error } = await api.DELETE('/api/attachments/{uid}', { params: { path: { uid } } })
      if (error) throw error
    }
  },
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memos'] }),
})

// Delete a whole memo (owner-only on the server) and refresh the feed.
const deleteMemo = useMutation({
  mutationFn: async (uid: string) => {
    const { error } = await api.DELETE('/api/memos/{uid}', { params: { path: { uid } } })
    if (error) throw error
  },
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memos'] }),
})

// Infinite scroll: load the next page when a sentinel below the list scrolls into view.
const sentinel = ref<HTMLElement | null>(null)
let observer: IntersectionObserver | null = null

onMounted(() => {
  if (typeof IntersectionObserver === 'undefined' || !sentinel.value) return
  observer = new IntersectionObserver((entries) => {
    if (entries[0]?.isIntersecting && hasNextPage.value && !isFetchingNextPage.value) {
      void fetchNextPage()
    }
  })
  observer.observe(sentinel.value)
})
onUnmounted(() => observer?.disconnect())
</script>

<template>
  <div class="flex flex-col gap-3" data-test="memo-feed">
    <template v-if="isPending">
      <MemoCardSkeleton v-for="n in 4" :key="n" />
    </template>
    <p v-else-if="isError" class="text-muted-color text-sm">Couldn't load memos.</p>
    <p v-else-if="!memos.length" class="text-muted-color text-sm" data-test="feed-empty">
      No memos here yet.
    </p>
    <template v-for="group in days" :key="group.key">
      <h3
        class="text-xs font-medium uppercase tracking-wide text-muted-color pt-2"
        data-test="day-header"
      >
        {{ group.label }}
      </h3>
      <MemoCard
        v-for="m in group.items"
        :key="m.uid"
        :memo="m"
        :restorable="restorable"
        @restore="emit('restore', $event)"
        @task-toggle="toggleTask.mutate($event)"
        @toggle-reaction="toggleReaction.mutate($event)"
        @delete-attachment="deleteAttachment.mutate($event)"
        @delete-attachments="deleteAttachments.mutate($event)"
        @delete-memo="deleteMemo.mutate($event)"
        @add-print="addPrint.mutate($event)"
        @edit="editMemo.mutate($event)"
        @retry="onTileRetry"
      />
    </template>
    <div ref="sentinel" class="h-4" data-test="sentinel" />
    <p v-if="isFetchingNextPage" class="text-muted-color text-sm">Loading more…</p>
  </div>
</template>
