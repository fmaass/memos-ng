<script setup lang="ts">
import { useQueryClient } from '@tanstack/vue-query'
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import CaptureEditor from '@/components/CaptureEditor.vue'
import MemoFeed from '@/components/MemoFeed.vue'
import { filterFromSearchParams, filterToQuery } from '@/lib/memoFilter'

// Refetch the feed when a memo is posted so it appears immediately. (Live media status is handled
// app-wide by useMediaEvents in App.vue.)
const queryClient = useQueryClient()
function onSaved() {
  void queryClient.invalidateQueries({ queryKey: ['memos'] })
}

// The feed honours any filter present in the URL (e.g. a ?tag= from the sidebar tag list), parsed
// through the shared memoFilter module so Home and Search agree on one serialization.
const route = useRoute()
const query = computed(() => filterToQuery(filterFromSearchParams(route.query)))
</script>

<template>
  <div class="max-w-2xl mx-auto p-4 flex flex-col gap-4" data-test="view-home">
    <CaptureEditor @saved="onSaved" />
    <MemoFeed :query="query" />
  </div>
</template>
