<script setup lang="ts">
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import SelectButton from 'primevue/selectbutton'
import { computed, onUnmounted, ref, watch } from 'vue'

import JustifiedGallery from '@/components/JustifiedGallery.vue'
import { useApi } from '@/composables/useApi'
import { type AttachPrintPayload, useMediaLightbox } from '@/composables/useMediaLightbox'
import { fetchPrintMasters, usePrintMaster } from '@/composables/usePrintMaster'
import type { MemoQuery } from '@/composables/useTimeline'
import { shouldRemint } from '@/lib/imageRetry'
import { selectPrintMaster, toPrintMasterInfo } from '@/lib/printMaster'

const api = useApi()
const queryClient = useQueryClient()
const confirm = useConfirm()
const toast = useToast()
const filter = ref<'all' | 'image' | 'video'>('all')
const options = [
  { label: 'All', value: 'all' },
  { label: 'Images', value: 'image' },
  { label: 'Videos', value: 'video' },
]

const { data, isPending, isFetching, refetch } = useQuery({
  queryKey: computed(() => ['attachments', filter.value]),
  queryFn: async () => {
    const query: MemoQuery = {}
    if (filter.value !== 'all') query.type = filter.value
    const { data, error } = await api.GET('/api/attachments', { params: { query } })
    if (error) throw error
    return data
  },
})

const items = computed(() => data.value ?? [])

let lastRemint = 0
function onTileRetry() {
  if (!shouldRemint(Date.now(), lastRemint, isFetching.value)) return
  lastRemint = Date.now()
  void refetch()
}

// Shared with the memo feed's card action — the composable carries the success/error toasts and
// refreshes this view's own attachment list (MEMNG-74/75).
const printMaster = usePrintMaster(['attachments'])

const lightbox = useMediaLightbox()
function openAt(index: number) {
  lightbox.open(items.value, index, {
    onDelete,
    onAttachPrint,
    onRemovePrint,
    loadPrintMaster: async (uid) => toPrintMasterInfo(selectPrintMaster(await fetchPrintMasters(uid))),
  })
}
onUnmounted(() => lightbox.destroy())

// The list refetched after an attach/remove — what the open viewer holds about the master is stale.
watch(items, () => lightbox.refreshPrintMaster())

// Replacing deletes the master that is there, so it is confirmed (naming the file that will
// overwrite it); a first attach destroys nothing and goes straight through.
function onAttachPrint({ uid, file, variantKey, replacing }: AttachPrintPayload) {
  if (!replacing) {
    printMaster.attach({ uid, file, variantKey })
    return
  }
  confirm.require({
    header: 'Replace print master',
    message: `Replace the print master with “${file.name}”? The current one is deleted.`,
    icon: 'pi pi-exclamation-triangle',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Replace' },
    accept: () => printMaster.attach({ uid, file, variantKey }),
  })
}

function onRemovePrint({ uid, variantKey }: { uid: string; variantKey: string }) {
  confirm.require({
    header: 'Remove print master',
    message: 'Remove this photo’s print master? The photo itself stays.',
    icon: 'pi pi-exclamation-triangle',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Remove', severity: 'danger' },
    accept: () => printMaster.remove({ uid, variantKey }),
  })
}

function onDelete(uid: string) {
  confirm.require({
    header: 'Delete media',
    message: 'Delete this media permanently? This cannot be undone.',
    icon: 'pi pi-exclamation-triangle',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Delete', severity: 'danger' },
    accept: async () => {
      const { error } = await api.DELETE('/api/attachments/{uid}', { params: { path: { uid } } })
      if (error) {
        toast.add({ severity: 'error', summary: 'Delete failed', life: 4000 })
        return
      }
      await queryClient.invalidateQueries({ queryKey: ['attachments'] })
    },
  })
}
</script>

<template>
  <section class="mx-auto flex max-w-6xl flex-col gap-4 p-4" data-test="view-resources">
    <h1 class="text-2xl font-semibold">Resources</h1>
    <SelectButton
      v-model="filter"
      :options="options"
      option-label="label"
      option-value="value"
      :allow-empty="false"
      aria-label="Filter media type"
      data-test="type-filter"
    />
    <p v-if="isPending" class="text-sm text-muted-color">Loading…</p>
    <p v-else-if="!items.length" class="text-sm text-muted-color" data-test="resources-empty">
      No attachments yet.
    </p>
    <JustifiedGallery
      v-else
      :items="items"
      :target-height="200"
      @open="openAt"
      @retry="onTileRetry"
      @print-retry="printMaster.retry($event)"
    />
  </section>
</template>
