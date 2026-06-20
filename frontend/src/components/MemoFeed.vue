<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import MemoCard from '@/components/MemoCard.vue'
import { type MemoQuery, useTimeline } from '@/composables/useTimeline'

// Generic cursor-paginated memo feed. Home passes no query; Explore/Search pass filters;
// Archived passes restorable to surface a per-card Restore action.
const props = withDefaults(defineProps<{ query?: MemoQuery; restorable?: boolean }>(), {
  query: () => ({}),
  restorable: false,
})
const emit = defineEmits<{ restore: [uid: string] }>()

const { data, fetchNextPage, hasNextPage, isFetchingNextPage, isPending, isError } = useTimeline(
  () => props.query,
)

const memos = computed(() => data.value?.pages.flatMap((page) => page.items) ?? [])

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
    <p v-if="isPending" class="text-muted-color text-sm">Loading…</p>
    <p v-else-if="isError" class="text-muted-color text-sm">Couldn't load memos.</p>
    <p v-else-if="!memos.length" class="text-muted-color text-sm" data-test="feed-empty">
      No memos here yet.
    </p>
    <MemoCard
      v-for="m in memos"
      :key="m.uid"
      :memo="m"
      :restorable="restorable"
      @restore="emit('restore', $event)"
    />
    <div ref="sentinel" class="h-4" data-test="sentinel" />
    <p v-if="isFetchingNextPage" class="text-muted-color text-sm">Loading more…</p>
  </div>
</template>
