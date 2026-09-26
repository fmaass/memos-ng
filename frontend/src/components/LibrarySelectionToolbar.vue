<script setup lang="ts">
import { computed } from 'vue'

// The dock while the Library selects (it replaces the dock outright, see useDockOverride): share on
// the left, the count in the middle, delete on the right. The count's status region stays mounted for
// the whole of select mode so a screen reader hears each change; the buttons name the count too.
const props = defineProps<{
  count: number
  /** The platform can share files (navigator.canShare); without it there is no share button. */
  canShare: boolean
  sharing: boolean
  deleting: boolean
}>()
const emit = defineEmits<{ share: []; delete: [] }>()

const noun = computed(() => (props.count === 1 ? 'item' : 'items'))
const label = computed(() => `${props.count} ${props.count === 1 ? 'Item' : 'Items'} Selected`)
</script>

<template>
  <div class="flex h-full w-[min(88vw,24rem)] items-center justify-between gap-2" data-test="library-selection-toolbar">
    <button
      v-if="canShare"
      type="button"
      class="glass flex aspect-square h-full items-center justify-center rounded-full text-primary disabled:opacity-50"
      :aria-label="`Share ${count} ${noun}`"
      :aria-busy="sharing ? 'true' : undefined"
      :disabled="count === 0 || sharing"
      data-test="selection-share"
      @click="emit('share')"
    >
      <i :class="sharing ? 'pi pi-spinner pi-spin' : 'pi pi-upload'" aria-hidden="true" />
    </button>
    <span v-else class="aspect-square h-full" aria-hidden="true" />
    <span
      class="min-w-0 flex-1 truncate text-center text-sm font-semibold"
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-test="selection-count"
    >{{ label }}</span>
    <button
      type="button"
      class="glass flex aspect-square h-full items-center justify-center rounded-full text-red-500 disabled:opacity-50 dark:text-red-400"
      :aria-label="`Delete ${count} ${noun}`"
      :aria-busy="deleting ? 'true' : undefined"
      :disabled="count === 0 || deleting"
      data-test="selection-delete"
      @click="emit('delete')"
    >
      <i :class="deleting ? 'pi pi-spinner pi-spin' : 'pi pi-trash'" aria-hidden="true" />
    </button>
  </div>
</template>
