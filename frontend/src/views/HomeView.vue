<script setup lang="ts">
import { useQueryClient } from '@tanstack/vue-query'
import { computed, inject, ref } from 'vue'
import { useRoute } from 'vue-router'

import CaptureEditor from '@/components/CaptureEditor.vue'
import MemoFeed from '@/components/MemoFeed.vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { PHONE_QUERY, TIMELINE_COMPOSER_KEY } from '@/composables/useTimelineComposer'
import { CONTENT_COLUMN } from '@/lib/layout'
import { filterFromSearchParams, filterToQuery } from '@/lib/memoFilter'

// Refetch the feed when a memo is posted so it appears immediately. (Live media status is handled
// app-wide by useMediaEvents in App.vue.)
const queryClient = useQueryClient()
function onSaved() {
  void queryClient.invalidateQueries({ queryKey: ['memos'] })
  composerOpen.value = false
}

// No editor at rest (plan decision 13): the header's new-memo button opens it — inline above the
// feed on md+, as the header's own dialog below md.
const composerOpen = inject(TIMELINE_COMPOSER_KEY, ref(false))
const isPhone = useMediaQuery(PHONE_QUERY)

// The feed honours any filter present in the URL (e.g. a ?tag= from the sidebar tag list), parsed
// through the shared memoFilter module so Home and Search agree on one serialization.
const route = useRoute()
const query = computed(() => filterToQuery(filterFromSearchParams(route.query)))
</script>

<template>
  <div class="mx-auto p-4 flex flex-col gap-4" :class="CONTENT_COLUMN" data-test="view-home">
    <CaptureEditor v-if="composerOpen && !isPhone" @saved="onSaved" />
    <MemoFeed :query="query" />
  </div>
</template>
