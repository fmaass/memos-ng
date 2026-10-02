<script setup lang="ts">
import { computed } from 'vue'

import type { components } from '@/api/schema'
import MediaTile from '@/components/MediaTile.vue'
import { printMasterUploads } from '@/composables/usePrintMaster'
import { dayHeadingParts } from '@/lib/librarySections'
import { daySlots, type MosaicSlot } from '@/lib/mosaic'
import { printChipState } from '@/lib/printMaster'

// One day of the All view (plan decision 13): a heading row, then the day's pictures as one mosaic
// block cycling the Months frame patterns, offset by the day's index in the whole loaded list. The
// tiles are the gallery's MediaTile, wired as JustifiedGallery wires them.
type DayAttachment = Pick<
  components['schemas']['AttachmentListItem'],
  | 'uid'
  | 'mime'
  | 'status'
  | 'blurhash'
  | 'width'
  | 'height'
  | 'thumbnailUrl'
  | 'previewUrl'
  | 'posterUrl'
  | 'webUrl'
> & {
  renditions?: components['schemas']['RenditionItem'][]
  mine?: boolean
  filename?: string
}

const props = withDefaults(
  defineProps<{
    /** `YYYY-MM-DD` in the library zone, or the undated key. */
    day: string
    items: DayAttachment[]
    /** The day's index across the whole loaded list, so the cycle runs on across months. */
    dayIndex: number
    /** The day's bucket count; null leaves the cell empty (buckets pending, failed, or undated). */
    count?: number | null
    /** The heading's scroll-margin-top, so a jump that lands on it clears what sticks above. */
    headingScrollMargin?: string
    selecting?: boolean
    selected?: ReadonlySet<string>
  }>(),
  { count: null, selecting: false, selected: () => new Set<string>() },
)
const emit = defineEmits<{
  open: [index: number]
  select: [uid: string]
  retry: [uid: string]
  'print-retry': [uid: string]
}>()

const heading = computed(() => dayHeadingParts(props.day))

// A tile's rendered width as a share of the block: on a phone the block spans the viewport less its
// 12 px insets, on a desktop the 720 px column less the same.
const SHARE: Record<MosaicSlot, number> = {
  'hero-left': 2 / 3,
  'hero-right': 2 / 3,
  'side-right': 1 / 3,
  'side-left': 1 / 3,
  square: 1 / 3,
  wide: 1,
}
function sizesFor(slot: MosaicSlot): string {
  const share = SHARE[slot]
  return `(min-width: 768px) ${Math.round(696 * share)}px, calc((100vw - 24px) * ${share.toFixed(4)})`
}

const tiles = computed(() => {
  const slots = daySlots(props.items.length, props.dayIndex)
  return props.items.map((item, index) => ({ item, index, slot: slots[index]! }))
})

// Print-master state per photo: an orphan's failed upload is retryable only here (see
// JustifiedGallery).
const printUploads = printMasterUploads()
function printChipFor(att: DayAttachment) {
  return printChipState(att, printUploads.get(att.uid))
}

// THE gate between the two meanings of a tap, as in JustifiedGallery: while selecting nothing opens.
function onOpen(index: number) {
  if (props.selecting) return
  emit('open', index)
}
function selectionFor(att: DayAttachment) {
  if (!props.selecting) return null
  return { selected: props.selected.has(att.uid), selectable: att.mine === true, label: att.filename || att.uid }
}
</script>

<template>
  <!-- Edge to edge on a phone (the view pads by 1rem), a centred 720 px column on a desktop, as the
       Months mosaic sits. -->
  <section class="-mx-4 flex flex-col md:mx-auto md:w-full md:max-w-[720px]" data-test="day-group">
    <div
      class="flex items-baseline gap-2 px-3 pb-2"
      :data-day="day"
      :style="{ scrollMarginTop: headingScrollMargin }"
      data-test="day-heading"
    >
      <span class="font-bold">{{ heading.weekday }}</span>
      <span v-if="heading.date" class="text-muted-color">{{ heading.date }}</span>
      <span class="ml-auto text-sm text-muted-color tabular-nums" data-test="day-count">{{ count ?? '' }}</span>
    </div>
    <!-- MediaTile gets no width/height: the slot gives the box its shape, which an intrinsic ratio
         would override. -->
    <div class="day-block">
      <MediaTile
        v-for="tile in tiles"
        :key="tile.item.uid"
        class="day-tile"
        :class="`slot-${tile.slot}`"
        :data-uid="tile.item.uid"
        :data-slot="tile.slot"
        :att="tile.item"
        :sizes="sizesFor(tile.slot)"
        :print-chip="printChipFor(tile.item)"
        :selection="selectionFor(tile.item)"
        @open="onOpen(tile.index)"
        @select="emit('select', $event)"
        @retry="emit('retry', $event)"
        @print-retry="emit('print-retry', $event)"
      />
    </div>
  </section>
</template>

<style scoped>
/* DayMosaic's slot mapping, in a block inset 12 px with rounded corners and hairline gaps (mockup
   §2: margin 0 12px 22px, gap 2px, radius 12px). Dense packing lets a hero right of its squares (C)
   take the top of its band. */
.day-block {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  grid-auto-flow: row dense;
  gap: 2px;
  margin: 0 12px 22px;
  border-radius: 12px;
  overflow: hidden;
}
.day-tile {
  border-radius: 0;
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
