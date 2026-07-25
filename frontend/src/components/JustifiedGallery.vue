<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'

import type { components } from '@/api/schema'
import { useElementWidth } from '@/composables/useElementWidth'
import { nextRetrySrc } from '@/lib/imageRetry'
import { justifyRows } from '@/lib/justified'
import { blurhashToDataUrl } from '@/lib/media'

type AttachmentStatus = components['schemas']['AttachmentStatus']

interface GalleryAttachment {
  uid: string
  mime: string
  status: AttachmentStatus
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
const emit = defineEmits<{ open: [index: number]; retry: [uid: string] }>()

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

function baseSrc(att: GalleryAttachment): string {
  // Small thumb tier in the grid (the lightbox loads the larger preview).
  return att.mime.startsWith('video/') ? att.posterUrl : att.thumbnailUrl
}

// Resources owns its own query, so exhausted cache-bust retries bubble a re-mint request there.
const attempts = ref<Record<string, number>>({})
const retrySrc = ref<Record<string, string>>({})
const failed = ref<Record<string, boolean>>({})
function srcFor(att: GalleryAttachment): string {
  return retrySrc.value[att.uid] ?? baseSrc(att)
}
function onImgError(att: GalleryAttachment): void {
  const n = attempts.value[att.uid] ?? 0
  const next = nextRetrySrc(baseSrc(att), n)
  if (!next) {
    failed.value = { ...failed.value, [att.uid]: true }
    emit('retry', att.uid)
    return
  }
  attempts.value[att.uid] = n + 1
  retrySrc.value = { ...retrySrc.value, [att.uid]: next }
}
function retry(att: GalleryAttachment): void {
  attempts.value = { ...attempts.value, [att.uid]: 1 }
  retrySrc.value = { ...retrySrc.value, [att.uid]: nextRetrySrc(baseSrc(att), 0) ?? baseSrc(att) }
  failed.value = { ...failed.value, [att.uid]: false }
  emit('retry', att.uid)
}
// Same bucket-quantization gap as MediaTile: connectivity returning is the one
// signal that a failed tile's unchanged URL is worth a fresh attempt.
function onOnline(): void {
  for (const att of props.items) {
    if (failed.value[att.uid]) retry(att)
  }
}
window.addEventListener('online', onOnline)
onBeforeUnmount(() => window.removeEventListener('online', onOnline))

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
      <div
        v-for="li in row.items"
        :key="li.item.uid"
        class="group relative overflow-hidden rounded-md bg-surface-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary dark:bg-surface-800"
        :style="{ width: li.width + 'px', backgroundImage: bg(li.item), backgroundSize: 'cover' }"
      >
        <img
          :src="srcFor(li.item)"
          alt=""
          loading="lazy"
          decoding="async"
          class="h-full w-full object-cover transition duration-200 group-hover:scale-[1.04]"
          data-test="gallery-img"
          role="button"
          tabindex="0"
          :aria-label="`Open ${li.item.mime.startsWith('video/') ? 'video' : 'image'}`"
          @click="emit('open', li.index)"
          @keyup.enter="emit('open', li.index)"
          @error="onImgError(li.item)"
        />
        <span
          v-if="li.item.mime.startsWith('video/')"
          class="absolute inset-0 flex items-center justify-center"
        >
          <i class="pi pi-play-circle text-4xl text-white/90 drop-shadow-lg" aria-hidden="true" />
        </span>
        <div
          v-if="failed[li.item.uid]"
          class="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-surface-0/80 text-sm dark:bg-surface-950/80"
          data-test="gallery-failed"
        >
          <i class="pi pi-exclamation-triangle text-red-500 dark:text-red-400" aria-hidden="true" />
          <button class="text-primary underline" data-test="gallery-retry" @click="retry(li.item)">
            Retry
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
