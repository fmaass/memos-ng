<script setup lang="ts">
import Button from 'primevue/button'
import { computed, onUnmounted, ref, watch } from 'vue'

import { useReachability } from '@/composables/useReachability'

export interface BannerTimers {
  set: (fn: () => void, delayMs: number) => unknown
  clear: (handle: unknown) => void
}

// Recovery gets its own brief notice: a banner that simply vanishes is a change nobody
// perceives and no screen reader announces. Long enough to read, short enough never to become
// furniture.
const RECOVERED_NOTICE_MS = 3000

// Injected so the notice is tested without sleeping.
const props = withDefaults(defineProps<{ timers?: BannerTimers }>(), {
  timers: () => ({
    set: (fn: () => void, delayMs: number) => setTimeout(fn, delayMs),
    clear: (handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  }),
})

// Whether this component is drawing a row of chrome. The shell needs it to know which row owes
// the top safe-area inset.
const emit = defineEmits<{ occupied: [boolean] }>()

const { unreachable, retryNow } = useReachability()
const recovered = ref(false)
let handle: unknown = null

function cancel() {
  if (handle === null) return
  props.timers.clear(handle)
  handle = null
}

watch(unreachable, (now, before) => {
  if (now) {
    // A second drop inside the "Back online" window goes straight back to the bad news.
    cancel()
    recovered.value = false
    return
  }
  if (!before) return
  cancel()
  recovered.value = true
  handle = props.timers.set(() => {
    recovered.value = false
    handle = null
  }, RECOVERED_NOTICE_MS)
})

const occupied = computed(() => unreachable.value || recovered.value)
watch(occupied, (value) => emit('occupied', value), { immediate: true })

onUnmounted(cancel)

// The shared shape of a chrome row. It carries the top safe-area inset because while a row is
// showing IT is the top of the chrome — the header drops its own inset to match (AppShell).
const ROW =
  'flex items-center gap-2 text-sm border-b pt-[calc(0.5rem+env(safe-area-inset-top))] pb-2 pl-[calc(1rem+env(safe-area-inset-left))] pr-[calc(0.5rem+env(safe-area-inset-right))]'
</script>

<template>
  <!-- One live region, mounted for the whole life of the app. A role="status" element that is
       itself inserted by v-if is routinely NOT announced, because the region did not exist at
       the moment its content changed; only the CONTENT here is conditional. Empty it takes no
       height, so it costs the layout nothing while all is well. -->
  <div role="status" aria-live="polite" data-test="connection-live-region">
    <div
      v-if="unreachable"
      data-test="connection-banner"
      :class="[
        ROW,
        'bg-amber-100 text-amber-900 border-amber-300 dark:bg-amber-400/15 dark:text-amber-200 dark:border-amber-400/30',
      ]"
    >
      <i class="pi pi-exclamation-triangle shrink-0" aria-hidden="true" />
      <span class="flex-1 min-w-0">Can't reach the server. Retrying…</span>
      <Button
        text
        size="small"
        label="Retry now"
        data-test="connection-retry"
        class="!text-amber-900 dark:!text-amber-200"
        @click="retryNow"
      />
    </div>
    <div
      v-else-if="recovered"
      data-test="connection-recovered"
      :class="[
        ROW,
        'bg-emerald-100 text-emerald-900 border-emerald-300 dark:bg-emerald-400/15 dark:text-emerald-200 dark:border-emerald-400/30',
      ]"
    >
      <i class="pi pi-check-circle shrink-0" aria-hidden="true" />
      <span class="flex-1 min-w-0">Back online.</span>
    </div>
  </div>
</template>
