<script setup lang="ts">
import { computed, ref } from 'vue'

import type { components } from '@/api/schema'
import MediaTile from '@/components/MediaTile.vue'
import { useElementWidth } from '@/composables/useElementWidth'
import { printMasterUploads } from '@/composables/usePrintMaster'
import { justifyRows } from '@/lib/justified'
import { justifiedTileSizes } from '@/lib/mediaLayout'
import { printChipState } from '@/lib/printMaster'

// What a gallery tile needs, taken from the generated list item so both callers — the Resources
// list (AttachmentListItem) and the Places cluster drawer (MapMarker) — satisfy it field for field.
type GalleryAttachment = Pick<
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
> & { renditions?: components['schemas']['RenditionItem'][] }

const props = withDefaults(
  defineProps<{
    items: GalleryAttachment[]
    targetHeight?: number
    gap?: number
    /** A tile's scroll-margin-top, so a caller that scrolls a tile to the top can clear what sticks there. */
    tileScrollMargin?: string
  }>(),
  { targetHeight: 200, gap: 4 },
)
const emit = defineEmits<{
  open: [index: number]
  retry: [uid: string]
  'print-retry': [uid: string]
}>()

// Print-master state per photo. Resources is where ORPHAN attachments live — a photo with no memo
// card anywhere — so if a failed print-master upload were not retryable here it would be retryable
// nowhere: the toast is gone and there is no other tile for that photo.
const printUploads = printMasterUploads()
function printChipFor(att: GalleryAttachment) {
  return printChipState(att, printUploads.get(att.uid))
}

const root = ref<HTMLElement | null>(null)
const containerWidth = useElementWidth(root)

// Flatten the running index across rows so `open` maps back to the original items array.
const rows = computed(() => {
  const laid = justifyRows(props.items, containerWidth.value, props.targetHeight, props.gap)
  let i = 0
  return laid.map((row) => ({
    height: row.height,
    items: row.items.map((li) => ({ ...li, index: i++ })),
  }))
})
</script>

<template>
  <!-- Each tile is the feed's MediaTile, laid out exactly as MemoCard's justified rows: its native
       <button> is the whole activation target (the video overlay included, and Enter AND Space),
       and its own retry state resets when a re-mint hands it a new URL. MediaTile names the uid;
       this gallery promises its callers the INDEX into `items`, so it is translated here. -->
  <div ref="root" class="flex flex-col" :style="{ gap: gap + 'px' }" data-test="justified-gallery">
    <div
      v-for="(row, ri) in rows"
      :key="ri"
      class="flex"
      :style="{ gap: gap + 'px', height: row.height + 'px' }"
    >
      <MediaTile
        v-for="li in row.items"
        :key="li.item.uid"
        :data-uid="li.item.uid"
        :att="li.item"
        :width="li.item.width"
        :height="li.item.height"
        :sizes="justifiedTileSizes(li.width)"
        :print-chip="printChipFor(li.item)"
        :style="{ width: li.width + 'px', height: li.height + 'px', scrollMarginTop: tileScrollMargin }"
        @open="emit('open', li.index)"
        @retry="emit('retry', $event)"
        @print-retry="emit('print-retry', $event)"
      />
    </div>
  </div>
</template>
