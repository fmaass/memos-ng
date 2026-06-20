<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { justifyRows } from '@/lib/justified'
import { blurhashToDataUrl } from '@/lib/media'

interface GalleryAttachment {
  uid: string
  mime: string
  status: string
  blurhash: string | null
  width: number | null
  height: number | null
  thumbnailUrl: string
  posterUrl: string
}

const props = withDefaults(
  defineProps<{ items: GalleryAttachment[]; targetHeight?: number; gap?: number }>(),
  { targetHeight: 200, gap: 4 },
)
const emit = defineEmits<{ open: [index: number] }>()

const root = ref<HTMLElement | null>(null)
const containerWidth = ref(0)
let ro: ResizeObserver | null = null

onMounted(() => {
  if (!root.value) return
  containerWidth.value = root.value.clientWidth
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver((entries) => {
      containerWidth.value = entries[0].contentRect.width
    })
    ro.observe(root.value)
  }
})
onBeforeUnmount(() => ro?.disconnect())

// Flatten the running index across rows so `open` maps back to the original items array.
const rows = computed(() => {
  const laid = justifyRows(props.items, containerWidth.value, props.targetHeight, props.gap)
  let i = 0
  return laid.map((row) => ({
    height: row.height,
    items: row.items.map((li) => ({ ...li, index: i++ })),
  }))
})

function srcFor(att: GalleryAttachment): string {
  // Small thumb tier in the grid (the lightbox loads the larger preview).
  return att.mime.startsWith('video/') ? att.posterUrl : att.thumbnailUrl
}
function bg(att: GalleryAttachment): string | undefined {
  const url = blurhashToDataUrl(att.blurhash)
  return url ? `url(${url})` : undefined
}
</script>

<template>
  <div ref="root" class="flex flex-col" :style="{ gap: gap + 'px' }" data-test="justified-gallery">
    <div
      v-for="(row, ri) in rows"
      :key="ri"
      class="flex"
      :style="{ gap: gap + 'px', height: row.height + 'px' }"
    >
      <button
        v-for="li in row.items"
        :key="li.item.uid"
        type="button"
        class="group relative overflow-hidden rounded-md bg-surface-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary dark:bg-surface-800"
        :style="{ width: li.width + 'px', backgroundImage: bg(li.item), backgroundSize: 'cover' }"
        :aria-label="`Open ${li.item.mime.startsWith('video/') ? 'video' : 'image'}`"
        @click="emit('open', li.index)"
      >
        <img
          :src="srcFor(li.item)"
          alt=""
          loading="lazy"
          class="h-full w-full object-cover transition duration-200 group-hover:scale-[1.04]"
          data-test="gallery-img"
        />
        <span
          v-if="li.item.mime.startsWith('video/')"
          class="absolute inset-0 flex items-center justify-center"
        >
          <i class="pi pi-play-circle text-4xl text-white/90 drop-shadow-lg" aria-hidden="true" />
        </span>
      </button>
    </div>
  </div>
</template>
