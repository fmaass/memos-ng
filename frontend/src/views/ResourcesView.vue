<script setup lang="ts">
import { useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import SelectButton from 'primevue/selectbutton'
import { onUnmounted, ref, watch, watchEffect } from 'vue'
import { useRouter } from 'vue-router'

import JustifiedGallery from '@/components/JustifiedGallery.vue'
import { type LibraryFilter, useAttachmentLibrary } from '@/composables/useAttachmentLibrary'
import { useApi } from '@/composables/useApi'
import { type AttachPrintPayload, useMediaLightbox } from '@/composables/useMediaLightbox'
import { fetchPrintMasters, usePrintMaster } from '@/composables/usePrintMaster'
import { shouldLoadMore } from '@/lib/attachmentPaging'
import { shouldRemint } from '@/lib/imageRetry'
import { memoPath } from '@/lib/places/lightbox'
import { selectPrintMaster, toPrintMasterInfo } from '@/lib/printMaster'

const api = useApi()
const router = useRouter()
const queryClient = useQueryClient()
const confirm = useConfirm()
const toast = useToast()
const filter = ref<LibraryFilter>('all')
const options = [
  { label: 'All', value: 'all' },
  { label: 'Images', value: 'image' },
  { label: 'Videos', value: 'video' },
]

const {
  items,
  isPending,
  isFetching,
  isError,
  isFetchNextPageError,
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
  refetch,
} = useAttachmentLibrary(filter)

// One request at a time: the sentinel and the button can both ask, and a second call while the
// first is in flight is ignored instead of cancelling it (cancelRefetch: false).
function loadMore() {
  if (isFetchingNextPage.value) return
  void fetchNextPage({ cancelRefetch: false })
}
// A failed next page retries that page; a failed first load or refetch starts over.
function retryLoad() {
  if (isFetchNextPageError.value) loadMore()
  else void refetch()
}

// Infinite scroll like MemoFeed's sentinel, except the decision is re-taken whenever paging state
// changes (shouldLoadMore): the observer fires only when visibility CHANGES, so a first page that
// leaves the sentinel on screen would otherwise never load a second. The button below is the
// fallback wherever there is no IntersectionObserver.
const sentinel = ref<HTMLElement | null>(null)
const sentinelVisible = ref(false)
let observer: IntersectionObserver | null = null
watch(sentinel, (el) => {
  observer?.disconnect()
  observer = null
  sentinelVisible.value = false
  if (!el || typeof IntersectionObserver === 'undefined') return
  observer = new IntersectionObserver((entries) => {
    sentinelVisible.value = entries.some((e) => e.isIntersecting)
  })
  observer.observe(el)
})
onUnmounted(() => observer?.disconnect())
watchEffect(() => {
  if (
    shouldLoadMore({
      visible: sentinelVisible.value,
      hasNextPage: hasNextPage.value,
      isFetchingNextPage: isFetchingNextPage.value,
      isError: isError.value,
    })
  ) {
    loadMore()
  }
})

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
    // A photo found in this gallery is usually wanted in its context; the viewer offers the jump
    // only on a slide that names a memo, so the orphans here simply never show it.
    onShowInMemo: (memoUid) => {
      void router.push(memoPath({ memo_uid: memoUid }))
    },
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
    <!-- A failed load is never shown as an empty library (MEMNG-504): nothing loaded + an error is
         the error state; an error after tiles loaded keeps them and says so below them. -->
    <div
      v-else-if="isError && !items.length"
      class="flex flex-col items-start gap-2 text-sm text-muted-color"
      data-test="resources-error"
    >
      <p>Couldn't load your media.</p>
      <Button
        label="Retry"
        icon="pi pi-refresh"
        size="small"
        outlined
        :loading="isFetching"
        data-test="resources-retry"
        @click="retryLoad"
      />
    </div>
    <p v-else-if="!items.length" class="text-sm text-muted-color" data-test="resources-empty">
      No attachments yet.
    </p>
    <template v-else>
      <JustifiedGallery
        :items="items"
        :target-height="200"
        @open="openAt"
        @retry="onTileRetry"
        @print-retry="printMaster.retry($event)"
      />
      <div ref="sentinel" aria-hidden="true" data-test="resources-sentinel" />
      <div
        v-if="isError"
        class="flex items-center gap-2 text-sm text-muted-color"
        data-test="resources-page-error"
      >
        <span>Couldn't load more media.</span>
        <Button
          label="Retry"
          icon="pi pi-refresh"
          size="small"
          text
          :loading="isFetching"
          data-test="resources-page-retry"
          @click="retryLoad"
        />
      </div>
      <Button
        v-else-if="hasNextPage"
        label="Load more"
        size="small"
        outlined
        class="self-center"
        :loading="isFetchingNextPage"
        data-test="resources-load-more"
        @click="loadMore"
      />
    </template>
  </section>
</template>
