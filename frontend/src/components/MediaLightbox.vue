<script setup lang="ts">
import { computed, onBeforeUnmount, watch } from 'vue'

import { keyToIntent, stepIndex } from '@/lib/lightbox'

// Reuses the attachment shape from both the Resources list and a memo's attachments.
interface LightboxItem {
  uid: string
  mime: string
  mine?: boolean // owner → may delete
  previewUrl: string
  webUrl: string
  posterUrl: string
  downloadUrl: string
}

const props = defineProps<{ items: LightboxItem[]; modelValue: number | null }>()
const emit = defineEmits<{ 'update:modelValue': [v: number | null]; delete: [uid: string] }>()

const open = computed(() => props.modelValue !== null && props.modelValue < props.items.length)
const idx = computed(() => props.modelValue ?? 0)
const current = computed(() => (open.value ? props.items[idx.value] : null))
const isVideo = computed(() => current.value?.mime.startsWith('video/') ?? false)

function go(dir: -1 | 1) {
  if (props.modelValue === null) return
  emit('update:modelValue', stepIndex(props.modelValue, props.items.length, dir))
}
function close() {
  emit('update:modelValue', null)
}
function onKey(e: KeyboardEvent) {
  const intent = keyToIntent(e.key)
  if (!intent) return
  e.preventDefault()
  if (intent === 'close') close()
  else go(intent === 'next' ? 1 : -1)
}

let startX: number | null = null
function onTouchStart(e: TouchEvent) {
  startX = e.changedTouches[0].clientX
}
function onTouchEnd(e: TouchEvent) {
  if (startX === null) return
  const dx = e.changedTouches[0].clientX - startX
  if (Math.abs(dx) > 50) go(dx < 0 ? 1 : -1)
  startX = null
}

watch(
  open,
  (isOpen) => {
    if (typeof window === 'undefined') return
    if (isOpen) window.addEventListener('keydown', onKey)
    else window.removeEventListener('keydown', onKey)
  },
  { immediate: true },
)
onBeforeUnmount(() => {
  if (typeof window !== 'undefined') window.removeEventListener('keydown', onKey)
})
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open && current"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/90 backdrop-blur-sm"
      data-test="lightbox"
      @click.self="close"
      @touchstart="onTouchStart"
      @touchend="onTouchEnd"
    >
      <button
        class="absolute top-3 right-3 p-2 text-white/80 hover:text-white"
        aria-label="Close"
        @click="close"
      >
        <i class="pi pi-times text-2xl" aria-hidden="true" />
      </button>
      <a
        :href="current.downloadUrl"
        class="absolute top-3 right-14 p-2 text-white/80 hover:text-white"
        aria-label="Download original"
        download
        @click.stop
      >
        <i class="pi pi-download text-xl" aria-hidden="true" />
      </a>
      <button
        v-if="current.mine"
        class="absolute top-3 right-24 p-2 text-white/80 hover:text-red-400"
        aria-label="Delete"
        data-test="lightbox-delete"
        @click.stop="emit('delete', current.uid)"
      >
        <i class="pi pi-trash text-xl" aria-hidden="true" />
      </button>

      <button
        v-if="idx > 0"
        class="absolute left-1 sm:left-5 p-3 text-white/70 hover:text-white"
        aria-label="Previous"
        @click.stop="go(-1)"
      >
        <i class="pi pi-chevron-left text-3xl" aria-hidden="true" />
      </button>
      <button
        v-if="idx < items.length - 1"
        class="absolute right-1 sm:right-5 p-3 text-white/70 hover:text-white"
        aria-label="Next"
        @click.stop="go(1)"
      >
        <i class="pi pi-chevron-right text-3xl" aria-hidden="true" />
      </button>

      <video
        v-if="isVideo"
        :key="current.uid"
        :src="current.webUrl"
        :poster="current.posterUrl"
        controls
        autoplay
        class="max-h-[92vh] max-w-[94vw] rounded shadow-2xl"
        data-test="lightbox-video"
      />
      <img
        v-else
        :key="current.uid"
        :src="current.previewUrl"
        :alt="current.uid"
        class="max-h-[92vh] max-w-[94vw] rounded object-contain shadow-2xl"
        data-test="lightbox-image"
      />

      <span class="absolute bottom-3 left-1/2 -translate-x-1/2 text-sm text-white/60">
        {{ idx + 1 }} / {{ items.length }}
      </span>
    </div>
  </Teleport>
</template>
