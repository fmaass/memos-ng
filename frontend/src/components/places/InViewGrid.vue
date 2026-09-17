<script setup lang="ts">
import Button from 'primevue/button'
import { computed } from 'vue'

import type { MapMarker } from '@/lib/places/geo'
import { blurhashToDataUrl } from '@/lib/media'
import { newestFirst } from '@/lib/places/order'

const props = defineProps<{ markers: MapMarker[]; filtered: boolean }>()
// The tapped photo travels by uid, never by position: the tiles are sorted and the caller's list
// is not, so an index would open a different photo in the viewer.
const emit = defineEmits<{ open: [uid: string]; reset: [] }>()

// Newest first: the map is the filter, and what the phone shows under it reads like the feed.
const ordered = computed(() => newestFirst(props.markers))

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
        v-for="marker in ordered"
        :key="marker.uid"
        type="button"
        class="aspect-square overflow-hidden rounded bg-surface-200 dark:bg-surface-800"
        data-test="in-view-tile"
        @click="emit('open', marker.uid)"
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
