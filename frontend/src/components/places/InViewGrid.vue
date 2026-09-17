<script setup lang="ts">
import Button from 'primevue/button'
import { computed } from 'vue'

import type { MapMarker } from '@/lib/places/geo'
import { blurhashToDataUrl } from '@/lib/media'

const props = defineProps<{ markers: MapMarker[]; filtered: boolean }>()
const emit = defineEmits<{ open: [index: number]; reset: [] }>()

// Newest first: the map is the filter, and what the phone shows under it reads like the feed.
const ordered = computed(() =>
  [...props.markers].sort((a, b) => Date.parse(b.taken_at) - Date.parse(a.taken_at)),
)

function source(marker: MapMarker): string {
  return marker.mime.startsWith('video/') ? marker.posterUrl : marker.thumbnailUrl
}
</script>

<template>
  <section class="flex flex-col gap-2 p-2" data-test="in-view-grid">
    <div class="flex items-center justify-between gap-2">
      <span class="text-sm text-muted-color" data-test="in-view-count">
        {{ ordered.length }} {{ ordered.length === 1 ? 'photo' : 'photos' }} in view
      </span>
      <Button
        v-if="props.filtered"
        label="Show all in view"
        size="small"
        text
        data-test="in-view-reset"
        @click="emit('reset')"
      />
    </div>
    <div class="grid grid-cols-3 gap-1">
      <button
        v-for="(marker, index) in ordered"
        :key="marker.uid"
        type="button"
        class="aspect-square overflow-hidden rounded bg-surface-200 dark:bg-surface-800"
        data-test="in-view-tile"
        @click="emit('open', index)"
      >
        <img
          :src="source(marker)"
          :style="{ backgroundImage: `url(${blurhashToDataUrl(marker.blurhash) ?? ''})` }"
          class="h-full w-full bg-cover object-cover"
          alt=""
          loading="lazy"
        />
      </button>
    </div>
  </section>
</template>
