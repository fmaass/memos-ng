<script setup lang="ts">
import { computed } from 'vue'

import type { components } from '@/api/schema'
import { mosaicSections, mosaicSlots } from '@/lib/mosaic'

// The Months view (plan decision 9): the day buckets grouped by month, each month reduced to its six
// busiest days in date order and laid out by the frame cycle, offset by the month's index so
// consecutive months open on different frames. A tile is the day's cover and its day number — no
// count, no collage — and a tap, like the "more days" row, asks the view to jump into All.
const props = defineProps<{ days: components['schemas']['AttachmentBucket'][] }>()
const emit = defineEmits<{ jump: [day: string] }>()

const sections = computed(() =>
  mosaicSections(props.days).map((section, index) => {
    const slots = mosaicSlots(section.shown.length, index)
    return {
      ...section,
      tiles: section.shown.map((day, i) => ({ day, slot: slots[i]!, number: Number(day.key.slice(8)) })),
    }
  }),
)
</script>

<template>
  <!-- Edge to edge on a phone (the view pads by 1rem), a centred 720 px column on a desktop. -->
  <div class="-mx-4 flex flex-col gap-6 md:mx-auto md:w-full md:max-w-[720px]" data-test="day-mosaic">
    <section
      v-for="section in sections"
      :key="section.key"
      class="flex flex-col gap-2"
      :data-month="section.key"
      data-test="mosaic-section"
    >
      <h2 class="px-4 text-xl font-bold md:px-0" data-test="mosaic-title">{{ section.label }}</h2>
      <div class="mosaic-grid">
        <button
          v-for="tile in section.tiles"
          :key="tile.day.key"
          type="button"
          class="mosaic-tile bg-surface-200 dark:bg-surface-800"
          :class="`slot-${tile.slot}`"
          :data-slot="tile.slot"
          data-test="mosaic-tile"
          @click="emit('jump', tile.day.key)"
        >
          <img :src="tile.day.coverThumbnailUrl" alt="" loading="lazy" class="absolute inset-0 h-full w-full object-cover" />
          <span class="absolute top-1.5 left-2 text-lg font-semibold text-white drop-shadow-md" data-test="mosaic-day">{{
            tile.number
          }}</span>
        </button>
      </div>
      <button
        v-if="section.dropped > 0"
        type="button"
        class="self-start px-4 py-1 text-sm font-medium text-primary md:px-0"
        data-test="mosaic-more"
        @click="emit('jump', section.shown[0]!.key)"
      >
        and {{ section.dropped }} more {{ section.dropped === 1 ? 'day' : 'days' }}
      </button>
    </section>
  </div>
</template>

<style scoped>
/* Dense packing, because a hero that sits RIGHT of its squares (C) comes first in the list and must
   still take the top of its band; every band before the last is full, so there is no earlier hole
   for dense to fill. Every frame carries its own ratio, so a band cut short keeps its shape. */
.mosaic-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  grid-auto-flow: row dense;
  gap: 2px;
}
.mosaic-tile {
  position: relative;
  overflow: hidden;
  border-radius: 6px;
  aspect-ratio: 1;
}
.slot-hero-left {
  grid-column: 1 / span 2;
  grid-row: span 2;
}
.slot-side-right {
  grid-column: 3;
}
.slot-hero-right {
  grid-column: 2 / span 2;
  grid-row: span 2;
}
.slot-side-left {
  grid-column: 1;
}
.slot-wide {
  grid-column: 1 / -1;
  aspect-ratio: 16 / 9;
}
</style>
