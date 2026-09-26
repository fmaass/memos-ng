<script setup lang="ts">
import { useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { computed, nextTick, onUnmounted, ref, watch, watchEffect } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import DayMosaic from '@/components/DayMosaic.vue'
import JustifiedGallery from '@/components/JustifiedGallery.vue'
import LibraryHeader from '@/components/LibraryHeader.vue'
import LibrarySegment from '@/components/LibrarySegment.vue'
import YearCards from '@/components/YearCards.vue'
import { useAttachmentBuckets, useAttachmentLibrary } from '@/composables/useAttachmentLibrary'
import { useApi } from '@/composables/useApi'
import { useDockSegment } from '@/composables/useDockSegment'
import { type AttachPrintPayload, useMediaLightbox } from '@/composables/useMediaLightbox'
import { fetchPrintMasters, usePrintMaster } from '@/composables/usePrintMaster'
import { appendPage, shouldLoadMore } from '@/lib/attachmentPaging'
import { scrollToDay } from '@/lib/dayJump'
import { shouldRemint } from '@/lib/imageRetry'
import {
  fromSearchParams,
  type LibraryFilter,
  type LibraryView,
  sameLibraryFilter,
  toRouteQuery,
  viewFromQuery,
  viewToQuery,
} from '@/lib/libraryFilter'
import { groupByMonth } from '@/lib/librarySections'
import { daysOfYear } from '@/lib/mosaic'
import { memoPath } from '@/lib/places/lightbox'
import { selectPrintMaster, toPrintMasterInfo } from '@/lib/printMaster'

const api = useApi()
const route = useRoute()
const router = useRouter()
const queryClient = useQueryClient()
const confirm = useConfirm()
const toast = useToast()

// The filter lives in the URL (linkable, reload-stable), synced both ways like SearchView's: seeded
// from the route, written back on a change, re-seeded on a query-only navigation (back/forward).
// Equal filters serialize identically, which is the loop guard in both directions.
// The Years | Months | All segment rides the same query BESIDE the filter (the dock's pill writes it
// there too): each write carries both, so a filter change keeps the segment and a segment change
// keeps the filter, and neither query key — both built from the filter alone — moves on a segment.
const filter = ref<LibraryFilter>(fromSearchParams(route.query))
const view = ref<LibraryView>(viewFromQuery(route.query))
function writeRoute() {
  if (sameLibraryFilter(filter.value, fromSearchParams(route.query)) && view.value === viewFromQuery(route.query)) {
    return
  }
  void router.replace({ query: { ...toRouteQuery(filter.value), ...viewToQuery(view.value) } })
}
watch(filter, writeRoute)
function setView(next: LibraryView) {
  view.value = next
  writeRoute()
}
watch(
  () => route.query,
  () => {
    const fromUrl = fromSearchParams(route.query)
    if (!sameLibraryFilter(fromUrl, filter.value)) filter.value = fromUrl
    view.value = viewFromQuery(route.query)
  },
)

// A year card opens Months narrowed to that year — client-side, by the day keys' year prefix; the
// narrowing is this view's own and ends when Months is left.
const monthsYear = ref<string | null>(null)
watch(view, (v) => {
  if (v !== 'months') monthsYear.value = null
})
function openYear(year: string) {
  setView('months')
  monthsYear.value = year
}

const dayBuckets = useAttachmentBuckets('day', filter, () => view.value === 'months')
const yearBuckets = useAttachmentBuckets('year', filter, () => view.value === 'years')
const mosaicDays = computed(() => {
  const days = dayBuckets.data.value ?? []
  return monthsYear.value ? daysOfYear(days, monthsYear.value) : days
})
// What the Years or Months view shows, so both share one loading/error/empty ladder — a failed
// buckets load is never shown as an empty library (MEMNG-504).
const bucketView = computed(() => {
  const query = view.value === 'years' ? yearBuckets : dayBuckets
  return {
    pending: query.isPending.value,
    error: query.isError.value,
    fetching: query.isFetching.value,
    empty: (view.value === 'years' ? (yearBuckets.data.value ?? []) : mosaicDays.value).length === 0,
    retry: () => void query.refetch(),
  }
})

// Years | Months | All, shown by the shell's dock once the Library is scrolled.
useDockSegment(LibrarySegment)

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

// One gallery per Zurich calendar month, in the list's own (takenAt) order. Each gallery reports a
// tile index within its month; `start` maps it back onto the whole loaded list the viewer walks.
const sections = computed(() => {
  let start = 0
  return groupByMonth(items.value).map((section) => {
    const withStart = { ...section, start }
    start += section.items.length
    return withStart
  })
})

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
const jumping = ref(false)
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
  // A jump walks the list itself (below); the sentinel waits, so it cannot race the walk for the page
  // after the one holding the day.
  if (
    !jumping.value &&
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

// The jump from a Months day (or its "more days" row) into All, decision 3: All opens, the walk
// advances the SAME infinite query — same limit, same cursors — through the first page holding that
// Zurich day, and that day's first tile is scrolled into view. A later jump supersedes an earlier
// one (the token), so only the latest tap ever scrolls.
const root = ref<HTMLElement | null>(null)
let jumpToken = 0
onUnmounted(() => {
  jumpToken += 1
})
function settled(): Promise<void> {
  if (!isPending.value) return Promise.resolve()
  return new Promise((resolve) => {
    const stop = watch(isPending, (pending) => {
      if (pending) return
      stop()
      resolve()
    })
  })
}
const pager = {
  current: async () => {
    await settled()
    return { items: items.value, hasNext: hasNextPage.value }
  },
  // The page's own result, not the view's refs, which catch up a tick later. cancelRefetch: false
  // joins a request already in flight instead of cancelling it.
  fetchNext: async () => {
    const result = await fetchNextPage({ cancelRefetch: false })
    if (result.isError) return null
    return { items: appendPage(result.data?.pages.map((p) => p.items) ?? []), hasNext: result.hasNextPage }
  },
}
// The tile exists once the list refs caught up and the gallery measured its width, a few frames on.
async function tileFor(uid: string, token: number): Promise<HTMLElement | null> {
  for (let frame = 0; frame < 120 && token === jumpToken; frame++) {
    await nextTick()
    const tiles = root.value?.querySelectorAll<HTMLElement>('[data-uid]') ?? []
    const tile = [...tiles].find((el) => el.dataset.uid === uid)
    if (tile) return tile
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
  }
  return null
}
// Let the sentinel speak afresh: what it reported before the scroll is stale, and an observer only
// reports a CHANGE — re-observing makes it report where the sentinel is now.
function rearmSentinel() {
  sentinelVisible.value = false
  const el = sentinel.value
  if (!observer || !el) return
  observer.unobserve(el)
  observer.observe(el)
}
async function jumpToDay(day: string) {
  const token = ++jumpToken
  setView('all')
  jumping.value = true
  try {
    const uid = await scrollToDay(day, pager)
    if (!uid || token !== jumpToken) return
    const tile = await tileFor(uid, token)
    if (tile && token === jumpToken) tile.scrollIntoView({ block: 'center' })
  } finally {
    if (token === jumpToken) {
      jumping.value = false
      rearmSentinel()
    }
  }
}

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
  <section ref="root" class="mx-auto flex max-w-6xl flex-col gap-4 p-4" data-test="view-resources">
    <LibraryHeader v-model:filter="filter" />
    <div
      v-if="jumping"
      class="pointer-events-none fixed inset-x-0 top-1/2 z-20 flex justify-center"
      data-test="day-jump-spinner"
    >
      <i class="pi pi-spinner pi-spin text-3xl text-muted-color" aria-label="Loading" />
    </div>
    <template v-if="view !== 'all'">
      <p v-if="bucketView.pending" class="text-sm text-muted-color">Loading…</p>
      <div
        v-else-if="bucketView.error"
        class="flex flex-col items-start gap-2 text-sm text-muted-color"
        data-test="buckets-error"
      >
        <p>Couldn't load your media.</p>
        <Button
          label="Retry"
          icon="pi pi-refresh"
          size="small"
          outlined
          :loading="bucketView.fetching"
          data-test="buckets-retry"
          @click="bucketView.retry"
        />
      </div>
      <p v-else-if="bucketView.empty" class="text-sm text-muted-color" data-test="resources-empty">
        No attachments yet.
      </p>
      <!-- A phone keeps the cards full width; a desktop centres them in the mosaic's 720 px column. -->
      <div v-else-if="view === 'years'" class="md:mx-auto md:w-full md:max-w-[720px]">
        <YearCards :years="yearBuckets.data.value ?? []" @select="openYear" />
      </div>
      <DayMosaic v-else :days="mosaicDays" @jump="jumpToDay" />
    </template>
    <p v-else-if="isPending" class="text-sm text-muted-color">Loading…</p>
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
      <!-- The month header sticks under the shell's chrome, whose height the shell measures. -->
      <div v-for="section in sections" :key="section.key" class="flex flex-col gap-2">
        <h2
          class="sticky top-[var(--chrome-height,0px)] z-10 -mx-4 bg-surface-50/90 px-4 py-2 text-lg font-semibold backdrop-blur-sm dark:bg-surface-950/90"
          data-test="month-header"
        >
          {{ section.label }}
        </h2>
        <JustifiedGallery
          :items="section.items"
          :target-height="200"
          @open="openAt(section.start + $event)"
          @retry="onTileRetry"
          @print-retry="printMaster.retry($event)"
        />
      </div>
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
