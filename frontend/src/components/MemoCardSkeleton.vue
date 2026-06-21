<script setup lang="ts">
// A shimmer placeholder shaped like a real MemoCard, shown while the timeline loads so the open
// is never a blank screen. PrimeVue Skeleton animates a wave (shimmer) by default.
import Skeleton from 'primevue/skeleton'
</script>

<template>
  <article
    class="flex flex-col gap-3 rounded-xl border border-surface-200 p-4 dark:border-surface-700"
    data-test="memo-skeleton"
    aria-hidden="true"
  >
    <Skeleton width="6rem" height="0.7rem" />
    <Skeleton width="100%" height="0.9rem" />
    <Skeleton width="85%" height="0.9rem" />
    <Skeleton width="45%" height="0.9rem" />
    <div class="flex gap-2 pt-1">
      <Skeleton width="5.5rem" height="5.5rem" border-radius="0.5rem" />
      <Skeleton width="5.5rem" height="5.5rem" border-radius="0.5rem" />
      <Skeleton width="5.5rem" height="5.5rem" border-radius="0.5rem" />
    </div>
  </article>
</template>
