<script setup lang="ts">
import Button from 'primevue/button'
import Skeleton from 'primevue/skeleton'

// The map pane while the map itself is still a megabyte away — and when that megabyte never
// arrived. An empty pane reads as a broken feature, which is what the slow tunnel produced.
// Vue hands the loading state the SAME props as the map (markers, listeners); none of them belong
// on this element.
defineOptions({ inheritAttrs: false })
defineProps<{ failed?: boolean }>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div
    v-if="failed"
    class="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center"
    data-test="map-load-error"
  >
    <p class="text-muted-color">The map did not load.</p>
    <Button label="Try again" size="small" outlined data-test="map-retry" @click="$emit('retry')" />
  </div>
  <Skeleton v-else class="!absolute inset-0 !h-full !w-full" data-test="map-loading" />
</template>
