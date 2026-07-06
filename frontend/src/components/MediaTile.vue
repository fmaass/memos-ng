<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import type { components } from '@/api/schema'
import { nextRetrySrc, retryDelayMs } from '@/lib/imageRetry'
import { blurhashToDataUrl, tileKind } from '@/lib/media'

type AttachmentStatus = components['schemas']['AttachmentStatus']

// Only the fields a tile renders — so both MemoOut.attachments (AttachmentRef) and the
// Resources list (AttachmentListItem) satisfy it without a width/height/duration shape clash.
interface MediaAttachment {
  uid: string
  mime: string
  status: AttachmentStatus
  blurhash: string | null
  thumbnailUrl: string
  previewUrl: string
  posterUrl: string
  webUrl: string
}

const props = defineProps<{ att: MediaAttachment; progress?: number }>()
const emit = defineEmits<{ retry: [uid: string]; open: [uid: string] }>()

const kind = computed(() => tileKind(props.att.status, props.att.mime))
const blur = computed(() => blurhashToDataUrl(props.att.blurhash))

// The grid tier this tile loads (video tiles show the poster). Used as the retry base: a re-mint
// refetch delivers a new URL here, which resets the tile via the watch below.
const baseUrl = computed(() => (kind.value === 'video' ? props.att.posterUrl : props.att.thumbnailUrl))
const imgSrc = ref(baseUrl.value)
const attempt = ref(0)
const loadFailed = ref(false)
const showFailed = computed(() => kind.value === 'failed' || loadFailed.value)
let retryTimer: ReturnType<typeof setTimeout> | null = null

function clearRetryTimer() {
  if (retryTimer === null) return
  clearTimeout(retryTimer)
  retryTimer = null
}

// A fresh URL (e.g. after a parent re-mint refetch) clears the failed state and retries from scratch.
watch(baseUrl, (url) => {
  clearRetryTimer()
  imgSrc.value = url
  attempt.value = 0
  loadFailed.value = false
})
onBeforeUnmount(clearRetryTimer)

// A thumbnail failed to load: cache-bust and retry a couple times (transient blip). Once exhausted
// the signed URL is likely expired — its signature can't be cache-busted back to life — so show the
// failed state and ask the parent to re-mint fresh URLs by refetching the feed.
function onImgError() {
  if (retryTimer !== null) return
  const currentAttempt = attempt.value
  const next = nextRetrySrc(baseUrl.value, currentAttempt)
  const delay = retryDelayMs(currentAttempt)
  if (next && delay !== null) {
    attempt.value += 1
    retryTimer = setTimeout(() => {
      imgSrc.value = next
      retryTimer = null
    }, delay)
  } else {
    loadFailed.value = true
    emit('retry', props.att.uid)
  }
}

// Manual Retry: force a fresh request even if the URL is unchanged, and ask the parent to re-mint.
function manualRetry() {
  clearRetryTimer()
  attempt.value = 0
  loadFailed.value = false
  imgSrc.value = nextRetrySrc(baseUrl.value, 0) ?? baseUrl.value
  emit('retry', props.att.uid)
}

// Signed URLs are bucket-quantized, so a re-mint inside the same bucket returns the
// IDENTICAL url and the baseUrl watch never clears a failed tile — a tile that
// exhausted its retries while offline stayed failed although the URL works again
// (AC-A2 live probe, 2026-07-06). Connectivity returning is the signal to retry.
function onOnline() {
  if (loadFailed.value) manualRetry()
}
window.addEventListener('online', onOnline)
onBeforeUnmount(() => window.removeEventListener('online', onOnline))
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
      <img
        :src="att.posterUrl"
        alt=""
        decoding="async"
        class="w-full h-full object-cover"
        data-test="tile-processing"
      />
      <div class="absolute inset-0 flex items-center justify-center">
        <span class="text-xs px-1.5 py-0.5 rounded bg-black/60 text-white">{{ progress ?? 0 }}%</span>
      </div>
    </template>

    <!-- failed (backend status OR the thumbnail/poster failed to load after retries): warning + retry.
         Checked before image/video so a load failure surfaces the recovery affordance. -->
    <div
      v-else-if="showFailed"
      class="w-full h-full flex flex-col items-center justify-center gap-1 text-sm"
      data-test="tile-failed"
    >
      <i class="pi pi-exclamation-triangle text-red-500 dark:text-red-400" aria-hidden="true" />
      <button class="text-primary underline" data-test="tile-retry" @click="manualRetry">
        Retry
      </button>
    </div>

    <!-- ready image: the blurhash shows instantly (tiny client-decoded preview) behind the real
         thumb, so the feed never renders as empty gray boxes while thumbnails download. -->
    <template v-else-if="kind === 'image'">
      <img
        v-if="blur"
        :src="blur"
        alt=""
        aria-hidden="true"
        class="absolute inset-0 w-full h-full object-cover"
        data-test="tile-blur"
      />
      <img
        :src="imgSrc"
        alt=""
        loading="lazy"
        decoding="async"
        class="relative w-full h-full object-cover cursor-pointer"
        data-test="tile-image"
        @click="emit('open', att.uid)"
        @error="onImgError"
      />
    </template>

    <button
      v-else-if="kind === 'video'"
      type="button"
      class="relative h-full w-full"
      data-test="tile-video"
      aria-label="Play video"
      @click="emit('open', att.uid)"
    >
      <img
        v-if="blur"
        :src="blur"
        alt=""
        aria-hidden="true"
        class="absolute inset-0 h-full w-full object-cover"
      />
      <img
        :src="imgSrc"
        alt=""
        loading="lazy"
        decoding="async"
        class="relative h-full w-full object-cover"
        @error="onImgError"
      />
      <span class="absolute inset-0 flex items-center justify-center">
        <i class="pi pi-play-circle text-4xl text-white/90 drop-shadow-lg" aria-hidden="true" />
      </span>
    </button>
  </div>
</template>
