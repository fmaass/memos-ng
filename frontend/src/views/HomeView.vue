<script setup lang="ts">
import { useQueryClient } from '@tanstack/vue-query'
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import CaptureEditor from '@/components/CaptureEditor.vue'
import MemoFeed from '@/components/MemoFeed.vue'
import type { MemoQuery } from '@/composables/useTimeline'

// Refetch the feed when a memo is posted so it appears immediately. (Live media status is handled
// app-wide by useMediaEvents in App.vue.)
const queryClient = useQueryClient()
function onSaved() {
  void queryClient.invalidateQueries({ queryKey: ['memos'] })
}

// A ?tag= from the sidebar tag list filters the feed to that tag.
const route = useRoute()
const query = computed<MemoQuery>(() => {
  const q: MemoQuery = {}
  const tag = route.query.tag
  if (typeof tag === 'string' && tag) q.tag = [tag]
  return q
})
</script>

<template>
  <div class="max-w-2xl mx-auto p-4 flex flex-col gap-4" data-test="view-home">
    <CaptureEditor @saved="onSaved" />
    <MemoFeed :query="query" />
  </div>
</template>
