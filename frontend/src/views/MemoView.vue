<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { useToast } from 'primevue/usetoast'
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import MemoCard from '@/components/MemoCard.vue'
import MemoCardSkeleton from '@/components/MemoCardSkeleton.vue'
import type { components } from '@/api/schema'
import { useApi } from '@/composables/useApi'
import { usePrintMaster } from '@/composables/usePrintMaster'
import { uploadsUnlinkableMessage } from '@/lib/composer'
import { CONTENT_COLUMN } from '@/lib/layout'

// /m/:uid — the canonical web URL of ONE memo: what gets shared out of the app, and what the
// browser falls back to on a device with no app installed (the same pattern the AASA applinks
// document claims, app/services/native_auth.py). It reuses MemoCard rather than rendering its own
// presentation, so a shared memo reads exactly as it does in the feed.
type Memo = components['schemas']['MemoOut']

const route = useRoute()
const api = useApi()
const queryClient = useQueryClient()
const toast = useToast()

const uid = computed(() => String(route.params.uid ?? ''))

// NOT keyed under ['memos']: that prefix holds infinite-query pages, and the SSE lifecycle walks
// every cache entry under it (`hasInFlight(data.pages…)` in useMediaEvents) — a single-memo entry
// there would throw on `.pages` and take the live media channel down app-wide.
const queryKey = computed(() => ['memo', uid.value] as const)

const { data: memo, isPending, isError, refetch } = useQuery({
  queryKey,
  queryFn: async (): Promise<Memo | null> => {
    const { data, error, response } = await api.GET('/api/memos/{uid}', {
      params: { path: { uid: uid.value } },
    })
    // 404 is an ANSWER here, not a failure: the memo is gone, or it is private and not the
    // reader's. Returning null keeps that case out of the error branch (and out of the query's
    // retries), so a stale link states what happened instead of looking like an outage.
    if (response.status === 404) return null
    if (error) throw error
    return data ?? null
  },
})

// A mutation on this page has to refresh BOTH this memo and the feeds behind it — the reader
// arrives here from a link and leaves into the app, and a card edited here would otherwise
// reappear stale on Home.
function refresh() {
  void queryClient.invalidateQueries({ queryKey: queryKey.value })
  void queryClient.invalidateQueries({ queryKey: ['memos'] })
}

const toggleTask = useMutation({
  mutationFn: async ({ uid: memoUid, content }: { uid: string; content: string }) => {
    const { error } = await api.PATCH('/api/memos/{uid}', {
      params: { path: { uid: memoUid } },
      body: { content },
    })
    if (error) throw error
  },
  onSuccess: refresh,
})

const toggleReaction = useMutation({
  mutationFn: async ({ uid: memoUid, emoji, add }: { uid: string; emoji: string; add: boolean }) => {
    const { error } = add
      ? await api.POST('/api/memos/{uid}/reactions', {
          params: { path: { uid: memoUid } },
          body: { emoji },
        })
      : await api.DELETE('/api/memos/{uid}/reactions/{emoji}', {
          params: { path: { uid: memoUid, emoji } },
        })
    if (error) throw error
  },
  onSuccess: refresh,
})

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
  onSuccess: refresh,
  // Same refusal the composer and the feed surface (MEMNG-113): uploads staged in an edit panel
  // left open past their expiry can no longer be linked, and the card has already cleared them.
  onError: (error) => {
    const lost = uploadsUnlinkableMessage(error)
    if (lost) toast.add({ severity: 'error', summary: 'Memo not saved', detail: lost, life: 10000 })
  },
})

const deleteAttachment = useMutation({
  mutationFn: async (attachmentUid: string) => {
    const { error } = await api.DELETE('/api/attachments/{uid}', {
      params: { path: { uid: attachmentUid } },
    })
    if (error) throw error
  },
  onSuccess: refresh,
})

// "Remove all" deletes SEQUENTIALLY — a parallel fan-out checks out one pooled DB connection per
// request and exhausts the bounded shared pool (the same rule MemoFeed's bulk delete follows).
const deleteAttachments = useMutation({
  mutationFn: async (uids: string[]) => {
    for (const attachmentUid of uids) {
      const { error } = await api.DELETE('/api/attachments/{uid}', {
        params: { path: { uid: attachmentUid } },
      })
      if (error) throw error
    }
  },
  onSuccess: refresh,
})

// Deleting the memo you are looking at leaves the permalink pointing at nothing — the refetch
// that follows answers 404 and this page states it, which is the truthful end state for the URL.
const deleteMemo = useMutation({
  mutationFn: async (memoUid: string) => {
    const { error } = await api.DELETE('/api/memos/{uid}', { params: { path: { uid: memoUid } } })
    if (error) throw error
  },
  onSuccess: refresh,
})

// Print masters run through the shared composable, pointed at THIS page's cache.
const printMaster = usePrintMaster(['memo'])

// A tile exhausted its in-place retries: its signed URL has expired, and only a refetch can mint
// a fresh signature. One memo, one request — no cooldown gate needed here.
function onTileRetry() {
  void refetch()
}
</script>

<template>
  <section class="mx-auto p-4 flex flex-col gap-3" :class="CONTENT_COLUMN" data-test="view-memo">
    <MemoCardSkeleton v-if="isPending" />
    <p v-else-if="isError" class="text-muted-color text-sm" data-test="memo-error">
      Couldn't load this memo.
    </p>
    <MemoCard
      v-else-if="memo"
      :memo="memo"
      :edit-pending="editMemo.isPending.value"
      @task-toggle="toggleTask.mutate($event)"
      @toggle-reaction="toggleReaction.mutate($event)"
      @delete-attachment="deleteAttachment.mutate($event)"
      @delete-attachments="deleteAttachments.mutate($event)"
      @delete-memo="deleteMemo.mutate($event)"
      @attach-print="printMaster.attach($event)"
      @remove-print="printMaster.remove($event)"
      @retry-print="printMaster.retry($event)"
      @edit="editMemo.mutate($event)"
      @retry="onTileRetry"
    />
    <p v-else class="text-muted-color text-sm" data-test="memo-missing">
      This memo isn't here — it may have been deleted, or it isn't shared with you.
    </p>
  </section>
</template>
