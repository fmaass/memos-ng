<script setup lang="ts">
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'

import JustifiedGallery from '@/components/JustifiedGallery.vue'
import type { MapMarker } from '@/lib/places/geo'

const props = defineProps<{ visible: boolean; markers: MapMarker[]; clusterId: number | null }>()
const emit = defineEmits<{
  'update:visible': [value: boolean]
  open: [index: number]
  'zoom-in': [clusterId: number]
}>()
</script>

<template>
  <Drawer
    :visible="props.visible"
    position="right"
    class="!w-[360px] max-w-full"
    data-test="cluster-drawer"
    @update:visible="emit('update:visible', $event)"
  >
    <template #header>
      <span class="font-semibold" data-test="cluster-count">
        {{ props.markers.length }} {{ props.markers.length === 1 ? 'photo' : 'photos' }} here
      </span>
    </template>
    <div class="flex flex-col gap-3">
      <!-- Zoom-in, not spiderfy: the map itself separates the pins, which keeps the cluster and
           its photos telling the same story (plan decision D4). -->
      <Button
        v-if="props.clusterId !== null"
        label="Zoom in"
        icon="pi pi-search-plus"
        size="small"
        outlined
        data-test="cluster-zoom"
        @click="emit('zoom-in', props.clusterId)"
      />
      <JustifiedGallery :items="props.markers" :target-height="120" @open="emit('open', $event)" />
    </div>
  </Drawer>
</template>
