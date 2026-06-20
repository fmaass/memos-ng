<script setup lang="ts">
import { computed } from 'vue'

import { blurhashToDataUrl, tileKind } from '@/lib/media'

// Only the fields a tile renders — so both MemoOut.attachments (AttachmentRef) and the
// Resources list (AttachmentListItem) satisfy it without a width/height/duration shape clash.
interface MediaAttachment {
  uid: string
  mime: string
  status: string
  blurhash: string | null
  previewUrl: string
  posterUrl: string
  webUrl: string
}

const props = defineProps<{ att: MediaAttachment; progress?: number }>()
const emit = defineEmits<{ retry: [uid: string] }>()

const kind = computed(() => tileKind(props.att.status, props.att.mime))
const blur = computed(() => blurhashToDataUrl(props.att.blurhash))
</script>

<template>
  <div
    class="relative aspect-square rounded-lg overflow-hidden bg-surface-100 dark:bg-surface-800"
    data-test="media-tile"
  >
    <!-- pending/uploading: blurhash placeholder (or shimmer) + Processing tag -->
    <template v-if="kind === 'placeholder'">
      <img
        v-if="blur"
        :src="blur"
        alt=""
        class="w-full h-full object-cover"
        data-test="tile-placeholder"
      />
      <div v-else class="w-full h-full animate-pulse" data-test="tile-placeholder" />
      <span class="absolute bottom-1 left-1 text-xs px-1.5 py-0.5 rounded bg-black/50 text-white">
        Processing
      </span>
    </template>

    <!-- processing: poster + percent overlay (% arrives via SSE) -->
    <template v-else-if="kind === 'processing'">
      <img :src="att.posterUrl" alt="" class="w-full h-full object-cover" data-test="tile-processing" />
      <div class="absolute inset-0 flex items-center justify-center">
        <span class="text-xs px-1.5 py-0.5 rounded bg-black/60 text-white">{{ progress ?? 0 }}%</span>
      </div>
    </template>

    <img
      v-else-if="kind === 'image'"
      :src="att.previewUrl"
      :alt="att.uid"
      class="w-full h-full object-cover"
      data-test="tile-image"
    />

    <video
      v-else-if="kind === 'video'"
      :poster="att.posterUrl"
      :src="att.webUrl"
      controls
      class="w-full h-full object-cover"
      data-test="tile-video"
    />

    <!-- failed: warning + retry -->
    <div
      v-else
      class="w-full h-full flex flex-col items-center justify-center gap-1 text-sm"
      data-test="tile-failed"
    >
      <i class="pi pi-exclamation-triangle text-red-500" />
      <button class="text-primary underline" data-test="tile-retry" @click="emit('retry', att.uid)">
        Retry
      </button>
    </div>
  </div>
</template>
