<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import type { components } from '@/api/schema'
import { nextRetrySrc, retryDelayMs, withSameBust } from '@/lib/imageRetry'
import { blurhashToDataUrl, tileKind } from '@/lib/media'
import { tileAspectRatio } from '@/lib/mediaLayout'
import { imageSrcset } from '@/lib/mediaTiers'

type AttachmentStatus = components['schemas']['AttachmentStatus']

// Only the fields a tile renders — so both MemoOut.attachments (AttachmentRef) and the
// Resources list (AttachmentListItem) satisfy it without hitting their `duration` shape clash.
// These dims are the FILE's, which is what the srcset descriptors describe; the width/height props
// below are the layout box's and can differ (a two-up tile is 1:1 whatever the photo is).
interface MediaAttachment {
  uid: string
  mime: string
  status: AttachmentStatus
  // SSE-only (see CachedAttachment): served by no endpoint, written onto the cached attachment by
  // applyMediaEvent, so it is absent until the stream reports on this attachment.
  progress_pct?: number
  blurhash: string | null
  width: number | null
  height: number | null
  thumbnailUrl: string
  previewUrl: string
  posterUrl: string
  webUrl: string
}

// width/height give the tile box its shape (the caller passes the attachment's intrinsic dims, or the
// uniform box of a multi-tile layout). Absent/unknown → the square box every tile used before,
// videos included (see `ratio`).
// sizes is the box's rendered width, which only the parent's layout knows (see tileSizes).
const props = defineProps<{
  att: MediaAttachment
  progress?: number
  width?: number | null
  height?: number | null
  sizes?: string
}>()
const emit = defineEmits<{ retry: [uid: string]; open: [uid: string] }>()

// The percentage the processing overlay shows. `progress` is the caller's explicit override (an
// upload the caller is itself driving); otherwise it comes from the attachment in the cache, where
// applyMediaEvent writes what the SSE channel reports for the running job (MEMNG-36). Nobody
// passed either before, so every transcoding video read 0% from start to finish.
const percent = computed(() => props.progress ?? props.att.progress_pct ?? 0)
const kind = computed(() => tileKind(props.att.status, props.att.mime))
const blur = computed(() => blurhashToDataUrl(props.att.blurhash))
// Videos take their probed ratio exactly like images (#37): the probe applies the container's
// rotation tag since v1.3.2 (displayed_size), so a portrait clip's stored dims now describe the
// same orientation the poster and web tier are written in. No dims (probe pending or failed) still
// means a null ratio, which is the neutral fallback box below.
// Residual: a clip probed BEFORE that fix kept ffprobe's CODED dims — landscape numbers for a
// portrait phone video — and nothing re-probes it (the worker writes dims once, in the fast lane at
// ingest; the metadata backfill excludes videos), so those tile at the wrong shape until a re-probe
// backfill ships.
const ratio = computed(() => tileAspectRatio(props.width, props.height))

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

// Both image tiers, each with the pixel width it truly holds, so a wide tile or a dense screen loads
// the mobile tier instead of stretching the 800px thumb. The thumb candidate is imgSrc rather than
// the raw URL, and the preview mirrors its cache-bust: a retry that busts only `src` would never
// re-request, because the browser picks from the srcset (see withSameBust).
const srcset = computed(() =>
  kind.value === 'image'
    ? imageSrcset(props.att.width, props.att.height, {
        thumbnailUrl: imgSrc.value,
        previewUrl: withSameBust(props.att.previewUrl, imgSrc.value),
      })
    : undefined,
)

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
    class="relative rounded-lg overflow-hidden bg-surface-100 dark:bg-surface-800"
    :class="{ 'aspect-square': ratio === null }"
    :style="ratio === null ? undefined : { aspectRatio: String(ratio) }"
    data-test="media-tile"
  >
    <!-- pending/uploading: blurhash placeholder (or shimmer) + Processing tag.
         The hash decodes square (32×32) but encodes the WHOLE frame, so it is stretched (object-fill)
         to whatever ratio the tile box has — cropping it to cover would throw away part of the preview. -->
    <template v-if="kind === 'placeholder'">
      <img
        v-if="blur"
        :src="blur"
        alt=""
        class="w-full h-full object-fill"
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
        <span class="text-xs px-1.5 py-0.5 rounded bg-black/60 text-white">{{ percent }}%</span>
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
         thumb, so the feed never renders as empty gray boxes while thumbnails download.
         A real <button> (same treatment as the video tile) so the viewer opens by keyboard too — a
         click handler on the <img> alone was mouse/touch-only. -->
    <button
      v-else-if="kind === 'image'"
      type="button"
      class="relative h-full w-full cursor-pointer focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary"
      data-test="tile-open"
      aria-label="Open image"
      @click="emit('open', att.uid)"
    >
      <img
        v-if="blur"
        :src="blur"
        alt=""
        aria-hidden="true"
        class="absolute inset-0 w-full h-full object-fill"
        data-test="tile-blur"
      />
      <img
        :src="imgSrc"
        :srcset="srcset"
        :sizes="srcset ? sizes : undefined"
        alt=""
        loading="lazy"
        decoding="async"
        class="relative w-full h-full object-cover"
        data-test="tile-image"
        @error="onImgError"
      />
    </button>

    <button
      v-else-if="kind === 'video'"
      type="button"
      class="relative h-full w-full cursor-pointer focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary"
      data-test="tile-video"
      aria-label="Play video"
      @click="emit('open', att.uid)"
    >
      <img
        v-if="blur"
        :src="blur"
        alt=""
        aria-hidden="true"
        class="absolute inset-0 h-full w-full object-fill"
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
