<script setup lang="ts">
import { useQueryClient } from '@tanstack/vue-query'
import Dialog from 'primevue/dialog'
import { computed } from 'vue'

import CaptureEditor from '@/components/CaptureEditor.vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { PHONE_QUERY } from '@/composables/useTimelineComposer'

// The Timeline's header controls (plan decision 13): settings on the left, new memo on the right,
// both glass, in the shell's sticky chrome so they stay while the feed scrolls. No search button
// (decision 4). Rendered as siblings inside the shell's header row: the new-memo button takes the
// row's last place (`order-last`) so the shell's own trailing controls sit between the two.
const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ 'update:open': [open: boolean] }>()

// Below md the editor opens here, as a dialog; on md+ the Timeline shows it inline above the feed.
const isPhone = useMediaQuery(PHONE_QUERY)
const dialogVisible = computed({
  get: () => props.open && isPhone.value,
  set: (visible: boolean) => {
    if (!visible) emit('update:open', false)
  },
})

const queryClient = useQueryClient()
function onSaved() {
  void queryClient.invalidateQueries({ queryKey: ['memos'] })
  emit('update:open', false)
}
</script>

<template>
  <RouterLink
    to="/settings"
    class="glass flex h-10 w-10 items-center justify-center rounded-full"
    aria-label="Settings"
    data-test="timeline-settings"
  >
    <i class="pi pi-cog" aria-hidden="true" />
  </RouterLink>
  <button
    type="button"
    class="glass order-last flex h-10 w-10 items-center justify-center rounded-full"
    :aria-label="open ? 'Close new memo' : 'New memo'"
    :aria-expanded="open"
    data-test="timeline-new-memo"
    @click="emit('update:open', !open)"
  >
    <i :class="open ? 'pi pi-times' : 'pi pi-pencil'" aria-hidden="true" />
  </button>
  <Dialog
    v-model:visible="dialogVisible"
    modal
    header="New memo"
    class="w-[calc(100vw-2rem)]"
    data-test="timeline-composer-dialog"
  >
    <CaptureEditor @saved="onSaved" />
  </Dialog>
</template>
