<script setup lang="ts">
import { computed } from 'vue'

import { useHeatmap } from '@/composables/useHeatmap'
import { buildHeatmap } from '@/lib/heatmap'

const { data } = useHeatmap()
const cells = computed(() => buildHeatmap(data.value ?? {}))

// intensity level 0–4 → background class
const LEVEL_CLASS = [
  'bg-surface-200 dark:bg-surface-700',
  'bg-primary-200 dark:bg-primary-900',
  'bg-primary-300 dark:bg-primary-700',
  'bg-primary-400 dark:bg-primary-500',
  'bg-primary-500 dark:bg-primary-400',
]
</script>

<template>
  <div class="px-3 mt-4" data-test="heatmap">
    <div class="text-xs uppercase tracking-wide text-muted-color pb-1">Activity</div>
    <div class="grid grid-flow-col grid-rows-7 gap-0.5">
      <span
        v-for="c in cells"
        :key="c.date"
        :title="`${c.date}: ${c.count}`"
        class="w-2.5 h-2.5 rounded-sm"
        :class="LEVEL_CLASS[c.level]"
        data-test="heat-cell"
      />
    </div>
  </div>
</template>
