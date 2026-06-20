<script setup lang="ts">
import { useMutation, useQueryClient } from '@tanstack/vue-query'

import MemoFeed from '@/components/MemoFeed.vue'
import { useApi } from '@/composables/useApi'
import type { MemoQuery } from '@/composables/useTimeline'

const api = useApi()
const queryClient = useQueryClient()
const archivedQuery: MemoQuery = { archived_only: true }

const restore = useMutation({
  mutationFn: async (uid: string) => {
    const { error } = await api.PATCH('/api/memos/{uid}', {
      params: { path: { uid } },
      body: { archived: false },
    })
    if (error) throw error
  },
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memos', archivedQuery] }),
})
</script>

<template>
  <section class="max-w-2xl mx-auto p-4 flex flex-col gap-3" data-test="view-archived">
    <h1 class="text-2xl font-semibold">Archived</h1>
    <MemoFeed :query="archivedQuery" restorable @restore="restore.mutate($event)" />
  </section>
</template>
