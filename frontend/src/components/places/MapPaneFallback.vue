<script setup lang="ts">
import Button from 'primevue/button'
import Skeleton from 'primevue/skeleton'
import { computed } from 'vue'

import type { MapLoadRung } from './asyncMap'

// The map pane while the map itself is still a megabyte away — and when that megabyte never
// arrived. An empty pane reads as a broken feature, which is what the slow tunnel produced.
// Vue hands the loading state the SAME props as the map (markers, listeners); none of them belong
// on this element.
defineOptions({ inheritAttrs: false })
const props = defineProps<{ failed?: boolean; rung?: MapLoadRung }>()
defineEmits<{ retry: [] }>()

// The button has to say what it will really do: on the last rung the pane replaces the document,
// which costs the viewer everything unsaved on the page.
const actionLabel = computed(() => (props.rung === 'reload' ? 'Reload the page' : 'Try again'))
</script>

<template>
  <div
    v-if="failed"
    class="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center"
    data-test="map-load-error"
  >
    <p class="text-muted-color">The map did not load.</p>
    <Button
      :label="actionLabel"
      size="small"
      outlined
      data-test="map-retry"
      @click="$emit('retry')"
    />
  </div>
  <Skeleton v-else class="!absolute inset-0 !h-full !w-full" data-test="map-loading" />
</template>
