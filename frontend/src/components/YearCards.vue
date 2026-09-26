<script setup lang="ts">
import type { components } from '@/api/schema'

// The Years view: one full-width 16:9 card per year bucket, its cover being the year's newest item
// (the server picks it), the year bottom-left and its item count as a badge.
defineProps<{ years: components['schemas']['AttachmentBucket'][] }>()
defineEmits<{ select: [year: string] }>()

const count = new Intl.NumberFormat('en')
</script>

<template>
  <div class="flex flex-col gap-3" data-test="year-cards">
    <button
      v-for="year in years"
      :key="year.key"
      type="button"
      class="relative block aspect-video w-full overflow-hidden rounded-xl bg-surface-100 dark:bg-surface-800"
      data-test="year-card"
      @click="$emit('select', year.key)"
    >
      <img
        :src="year.coverThumbnailUrl"
        alt=""
        loading="lazy"
        class="absolute inset-0 h-full w-full object-cover"
      />
      <span
        class="absolute bottom-3 left-4 text-3xl font-bold text-white drop-shadow-md"
        data-test="year-label"
        >{{ year.key }}</span
      >
      <span
        class="absolute right-4 bottom-3 rounded-full bg-black/50 px-2.5 py-0.5 text-sm font-semibold text-white backdrop-blur-sm"
        data-test="year-count"
        >{{ count.format(year.count) }}</span
      >
    </button>
  </div>
</template>
