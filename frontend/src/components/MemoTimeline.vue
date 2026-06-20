<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import MemoCard from '@/components/MemoCard.vue'
import { useMediaEvents } from '@/composables/useMediaEvents'
import { useTimeline } from '@/composables/useTimeline'

const { data, fetchNextPage, hasNextPage, isFetchingNextPage, isPending, isError } = useTimeline()

// Live media status: SSE patches the timeline cache so cards advance without a reload.
useMediaEvents()

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
  <div class="flex flex-col gap-3" data-test="timeline">
    <p v-if="isPending" class="text-muted-color text-sm">Loading…</p>
    <p v-else-if="isError" class="text-muted-color text-sm">Couldn't load memos.</p>
    <MemoCard v-for="m in memos" :key="m.uid" :memo="m" />
    <div ref="sentinel" class="h-4" data-test="sentinel" />
    <p v-if="isFetchingNextPage" class="text-muted-color text-sm">Loading more…</p>
  </div>
</template>
