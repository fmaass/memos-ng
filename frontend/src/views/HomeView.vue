<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import CaptureEditor from '@/components/CaptureEditor.vue'
import MemoFeed from '@/components/MemoFeed.vue'
import { useMediaEvents } from '@/composables/useMediaEvents'
import type { MemoQuery } from '@/composables/useTimeline'

// Live media status for the home timeline (patches the ['memos','timeline'] cache).
useMediaEvents()

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
    <CaptureEditor />
    <MemoFeed :query="query" />
  </div>
</template>
