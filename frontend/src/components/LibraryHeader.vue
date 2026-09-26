<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import LibraryOptionsMenu from '@/components/LibraryOptionsMenu.vue'
import type { LibraryFilter } from '@/lib/libraryFilter'

// The Library's header (plan decision 10): a large bold title, a subtitle naming what is shown, and
// on the right a round glass options button then a glass "Select" capsule. No search button
// (decision 4). While selecting, the two give way to a select-all toggle and a close button; what is
// selected is the view's (useLibrarySelection), this header only asks.
const props = withDefaults(
  defineProps<{
    filter: LibraryFilter
    selecting?: boolean
    /** Selection exists only in the All view; elsewhere the capsule is disabled. */
    canSelect?: boolean
    /** Every loaded item of mine is selected — the toggle's pressed state. */
    allSelected?: boolean
  }>(),
  { selecting: false, canSelect: true, allSelected: false },
)
const emit = defineEmits<{
  'update:filter': [filter: LibraryFilter]
  select: []
  'select-all': []
  'select-close': []
}>()

const KIND_LABEL = { image: 'Photos', video: 'Videos' } as const
const subtitle = computed(() =>
  [
    props.filter.type ? KIND_LABEL[props.filter.type] : 'All Items',
    props.filter.order === 'oldest' ? 'Oldest First' : 'Newest First',
    ...(props.filter.tag ? [`#${props.filter.tag}`] : []),
  ].join(' · '),
)

const menu = ref<InstanceType<typeof LibraryOptionsMenu> | null>(null)

// Entering or leaving select mode swaps out the very button that was activated, which would drop
// keyboard focus onto <body>. So focus follows the swap: to the select-all toggle on entry, back to
// the Select capsule on any exit (close, Escape, a view or filter change) — once the new buttons exist.
const selectAllButton = ref<HTMLButtonElement | null>(null)
const selectButton = ref<HTMLButtonElement | null>(null)
watch(
  () => props.selecting,
  async (on) => {
    await nextTick()
    ;(on ? selectAllButton : selectButton).value?.focus()
  },
)
</script>

<template>
  <div class="flex items-end gap-2" data-test="library-header">
    <div class="min-w-0 flex-1">
      <h1 class="text-3xl font-bold leading-tight">Library</h1>
      <p class="truncate text-sm text-muted-color" data-test="library-subtitle">{{ subtitle }}</p>
    </div>
    <template v-if="selecting">
      <button
        ref="selectAllButton"
        type="button"
        class="glass flex h-10 w-10 items-center justify-center rounded-full"
        :class="{ 'text-primary': allSelected }"
        aria-label="Select all of mine"
        :aria-pressed="allSelected ? 'true' : 'false'"
        data-test="library-select-all"
        @click="emit('select-all')"
      >
        <i class="pi pi-check-circle" aria-hidden="true" />
      </button>
      <button
        type="button"
        class="glass flex h-10 w-10 items-center justify-center rounded-full"
        aria-label="Done selecting"
        data-test="library-select-close"
        @click="emit('select-close')"
      >
        <i class="pi pi-times" aria-hidden="true" />
      </button>
    </template>
    <template v-else>
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
        ref="selectButton"
        type="button"
        class="glass h-10 rounded-full px-4 text-sm font-medium disabled:opacity-50"
        :disabled="!canSelect"
        data-test="library-select"
        @click="emit('select')"
      >
        Select
      </button>
    </template>
    <LibraryOptionsMenu ref="menu" :filter="filter" @update:filter="emit('update:filter', $event)" />
  </div>
</template>
