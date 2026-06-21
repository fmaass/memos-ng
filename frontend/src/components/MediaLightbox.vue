<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { keyToIntent, stepIndex } from '@/lib/lightbox'
import { type GestureState, initState, onDown, onMove, onUp } from '@/lib/lightboxGesture'

// Reuses the attachment shape from both the Resources list and a memo's attachments.
interface RenditionRef {
  variant_key: string
  label: string | null
  downloadUrl: string
}
interface LightboxItem {
  uid: string
  mime: string
  mine?: boolean // owner → may delete / attach a print master
  hasPrint?: boolean // has a print master of any kind (legacy print_path OR a rendition)
  hasLegacyPrint?: boolean // has the legacy single print_path → the /print download applies
  renditions?: RenditionRef[] // named print masters (glossy/matte/…), download-only
  previewUrl: string
  webUrl: string
  posterUrl: string
  downloadUrl: string
  printUrl?: string
}

const props = defineProps<{ items: LightboxItem[]; modelValue: number | null }>()
const emit = defineEmits<{
  'update:modelValue': [v: number | null]
  delete: [uid: string]
  'add-print': [payload: { uid: string; file: File }]
}>()

const printInput = ref<HTMLInputElement | null>(null)
const isImage = computed(() => current.value?.mime.startsWith('image/') ?? false)
function onPickPrint(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (file && current.value) emit('add-print', { uid: current.value.uid, file })
  if (printInput.value) printInput.value.value = ''
}

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

// Pinch-zoom + pan + swipe (arbitration is the pure reducer in lib/lightboxGesture). At scale 1 a
// horizontal drag navigates; once zoomed a drag pans and never navigates — replacing the old crude
// "any 50px drag flips the image" that made pinch-zoom impossible.
const imgEl = ref<HTMLImageElement | null>(null)
const gesture = ref<GestureState>(initState())
const smooth = ref(false) // ease on settle/double-tap; instant while dragging
const imgTransform = computed(
  () => `translate(${gesture.value.offsetX}px, ${gesture.value.offsetY}px) scale(${gesture.value.scale})`,
)
const pointers = new Map<number, { x: number; y: number }>()
function geom() {
  const el = imgEl.value
  return {
    viewW: window.innerWidth,
    viewH: window.innerHeight,
    imgW: el?.clientWidth ?? window.innerWidth,
    imgH: el?.clientHeight ?? window.innerHeight,
  }
}
function resetZoom() {
  gesture.value = initState()
  smooth.value = false
  pointers.clear()
}
function onPointerDown(e: PointerEvent) {
  imgEl.value?.setPointerCapture?.(e.pointerId)
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY })
  smooth.value = false
  gesture.value = onDown(gesture.value, [...pointers.values()], e.timeStamp).state
}
function onPointerMove(e: PointerEvent) {
  if (!pointers.has(e.pointerId)) return
  if (e.pointerType === 'mouse' && e.buttons === 0) return onPointerUp(e) // recover a missed up
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY })
  e.preventDefault()
  smooth.value = false
  gesture.value = onMove(gesture.value, [...pointers.values()], e.timeStamp, geom()).state
}
function onPointerUp(e: PointerEvent) {
  pointers.delete(e.pointerId)
  imgEl.value?.releasePointerCapture?.(e.pointerId)
  const r = onUp(gesture.value, [...pointers.values()], e.timeStamp, geom())
  gesture.value = r.state
  smooth.value = r.action.type === 'snapback' || r.action.type === 'transform'
  if (r.action.type === 'navigate') go(r.action.dir)
}

// Reset zoom/pan whenever the shown item changes or the lightbox closes.
watch([() => current.value?.uid, open], () => resetZoom())

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
      <!-- legacy single print master: download if present; gated on hasLegacyPrint (NOT hasPrint) so
           a photo with only named renditions doesn't show a button pointing at an absent print_path -->
      <a
        v-if="current.hasLegacyPrint && current.printUrl"
        :href="current.printUrl"
        class="absolute top-3 right-36 p-2 text-amber-300 hover:text-amber-200"
        aria-label="Download print version"
        title="Download print version"
        download
        data-test="lightbox-print-download"
        @click.stop
      >
        <i class="pi pi-print text-xl" aria-hidden="true" />
      </a>
      <!-- named print renditions (glossy/matte/…): download-only masters, hidden from the feed -->
      <div
        v-if="current.renditions && current.renditions.length"
        class="absolute top-14 right-3 flex flex-col items-end gap-1"
        data-test="lightbox-renditions"
      >
        <a
          v-for="r in current.renditions"
          :key="r.variant_key"
          :href="r.downloadUrl"
          class="rounded bg-black/40 px-2 py-1 text-xs text-amber-200 hover:text-amber-100"
          :aria-label="`Download ${r.label ?? 'print'} print`"
          :data-test="`lightbox-rendition-${r.variant_key}`"
          download
          @click.stop
        >
          <i class="pi pi-print mr-1" aria-hidden="true" />{{ r.label ?? 'print' }}
        </a>
      </div>
      <button
        v-if="current.mine && isImage && !current.hasLegacyPrint"
        class="absolute top-3 right-36 p-2 text-white/70 hover:text-amber-200"
        aria-label="Add print version"
        title="Attach a print-quality master"
        data-test="lightbox-add-print"
        @click.stop="printInput?.click()"
      >
        <i class="pi pi-print text-xl opacity-60" aria-hidden="true" />
      </button>
      <input
        ref="printInput"
        type="file"
        accept="image/*,.tif,.tiff"
        class="hidden"
        data-test="print-input"
        @change="onPickPrint"
      />

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
        ref="imgEl"
        :key="current.uid"
        :src="current.previewUrl"
        alt="Image"
        class="max-h-[92vh] max-w-[94vw] touch-none select-none rounded object-contain shadow-2xl"
        :style="{
          transform: imgTransform,
          transformOrigin: 'center center',
          transition: smooth ? 'transform 0.2s ease-out' : 'none',
          willChange: 'transform',
        }"
        data-test="lightbox-image"
        @pointerdown="onPointerDown"
        @pointermove="onPointerMove"
        @pointerup="onPointerUp"
        @pointercancel="onPointerUp"
        @click.stop
        @dblclick.prevent
      />

      <span class="absolute bottom-3 left-1/2 -translate-x-1/2 text-sm text-white/60">
        {{ idx + 1 }} / {{ items.length }}
      </span>
    </div>
  </Teleport>
</template>
