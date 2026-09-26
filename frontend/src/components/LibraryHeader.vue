<script setup lang="ts">
import { computed, ref } from 'vue'

import LibraryOptionsMenu from '@/components/LibraryOptionsMenu.vue'
import type { LibraryFilter } from '@/lib/libraryFilter'

// The Library's header (plan decision 10): a large bold title, a subtitle naming what is shown, and
// on the right a round glass options button then a glass "Select" capsule, inert until selection
// ships. No search button (decision 4).
const props = defineProps<{ filter: LibraryFilter }>()
const emit = defineEmits<{ 'update:filter': [filter: LibraryFilter] }>()

const KIND_LABEL = { image: 'Photos', video: 'Videos' } as const
const subtitle = computed(() =>
  [
    props.filter.type ? KIND_LABEL[props.filter.type] : 'All Items',
    props.filter.order === 'oldest' ? 'Oldest First' : 'Newest First',
    ...(props.filter.tag ? [`#${props.filter.tag}`] : []),
  ].join(' · '),
)

const menu = ref<InstanceType<typeof LibraryOptionsMenu> | null>(null)
</script>

<template>
  <div class="flex items-end gap-2" data-test="library-header">
    <div class="min-w-0 flex-1">
      <h1 class="text-3xl font-bold leading-tight">Library</h1>
      <p class="truncate text-sm text-muted-color" data-test="library-subtitle">{{ subtitle }}</p>
    </div>
    <button
      type="button"
      class="glass flex h-10 w-10 items-center justify-center rounded-full"
      aria-label="Library options"
      aria-haspopup="true"
      aria-controls="library-options"
      data-test="library-options"
      @click="menu?.toggle($event)"
    >
      <i class="pi pi-ellipsis-h" aria-hidden="true" />
    </button>
    <button
      type="button"
      class="glass h-10 rounded-full px-4 text-sm font-medium"
      aria-disabled="true"
      data-test="library-select"
    >
      Select
    </button>
    <LibraryOptionsMenu ref="menu" :filter="filter" @update:filter="emit('update:filter', $event)" />
  </div>
</template>
