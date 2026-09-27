<script setup lang="ts">
import { useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { computed, defineComponent, h, nextTick, onMounted, onUnmounted, ref, watch, watchEffect } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import DayGroup from '@/components/DayGroup.vue'
import DayMosaic from '@/components/DayMosaic.vue'
import LibraryHeader from '@/components/LibraryHeader.vue'
import LibrarySegment from '@/components/LibrarySegment.vue'
import LibrarySelectionToolbar from '@/components/LibrarySelectionToolbar.vue'
import YearCards from '@/components/YearCards.vue'
import { useAttachmentBuckets, useAttachmentLibrary } from '@/composables/useAttachmentLibrary'
import { useApi } from '@/composables/useApi'
import { deleteAttachment, useBulkDelete } from '@/composables/useBulkDelete'
import { useDockOverride, useDockSegment } from '@/composables/useDockSegment'
import { useLibrarySelection } from '@/composables/useLibrarySelection'
import { type AttachPrintPayload, useMediaLightbox } from '@/composables/useMediaLightbox'
import { fetchPrintMasters, usePrintMaster } from '@/composables/usePrintMaster'
import { useShareSelection } from '@/composables/useShareSelection'
import { appendPage, shouldLoadMore } from '@/lib/attachmentPaging'
import { scrollToDay } from '@/lib/dayJump'
import { shouldRemint } from '@/lib/imageRetry'
import {
  clearedLibraryFilter,
  fromSearchParams,
  type LibraryFilter,
  libraryEmptyState,
  type LibraryView,
  sameLibraryFilter,
  toRouteQuery,
  viewFromQuery,
  viewToQuery,
} from '@/lib/libraryFilter'
import { dayPillLabel, groupByDay, groupByMonth, topDay, UNDATED_KEY } from '@/lib/librarySections'
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
const emptyState = computed(() => libraryEmptyState(filter.value))
// A new object, never a mutation: the route write above watches the ref, not its fields.
function clearFilter() {
  filter.value = clearedLibraryFilter(filter.value)
}
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

// The day buckets feed Months' mosaic and All's day-heading counts (the whole day, not the loaded
// fragment of it), from one query under the same filter.
const dayBuckets = useAttachmentBuckets('day', filter, () => view.value === 'months' || view.value === 'all')
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

// Select mode (MEMNG-510), in the All view only: the selectable uids are the LOADED items of mine
// (useLibrarySelection), and anything that changes what All shows — leaving All, another kind or tag
// (another query), leaving the Library — ends select mode, clears it and dismisses a delete confirm
// it had open. `selectSession` fences that confirm: an accept from a session that has ended deletes
// nothing, and the batch it deletes is the one captured when it opened.
const selection = useLibrarySelection(items)
const { selecting, selected, allSelected } = selection
const bulkDelete = useBulkDelete()
const sharer = useShareSelection()
let selectSession = 0
let confirmOpen = false

function enterSelect() {
  if (view.value !== 'all' || selecting.value) return
  selectSession++
  selection.enter()
}
function leaveSelect() {
  selectSession++
  if (confirmOpen) {
    confirmOpen = false
    confirm.close()
  }
  selection.exit()
}
watch(view, (v) => {
  if (v !== 'all') leaveSelect()
})
watch(filter, leaveSelect)
onUnmounted(leaveSelect)

// Escape leaves select mode — but not while the confirm is up, where Escape is the dialog's.
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && selecting.value && !confirmOpen) leaveSelect()
}
watch(selecting, (on) =>
  on ? window.addEventListener('keydown', onKeydown) : window.removeEventListener('keydown', onKeydown),
)
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

function deleteSelected() {
  const batch = selection.capture()
  if (batch.length === 0 || confirmOpen || bulkDelete.pending.value) return
  const session = selectSession
  confirmOpen = true
  const n = batch.length
  confirm.require({
    header: `Delete ${n} ${n === 1 ? 'item' : 'items'}?`,
    message: 'They are deleted permanently. This cannot be undone.',
    icon: 'pi pi-exclamation-triangle',
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    acceptProps: { label: 'Delete', severity: 'danger' },
    // PrimeVue focuses Delete by default, so an Enter meant to dismiss would delete the batch.
    defaultFocus: 'reject',
    accept: async () => {
      confirmOpen = false
      if (session !== selectSession) return
      await bulkDelete.run(batch)
      if (session === selectSession) leaveSelect()
    },
    reject: () => {
      confirmOpen = false
    },
    // An Escape reaches PrimeVue's document listener, which closes the confirm and lands here, BEFORE
    // it bubbles on to this view's window listener: cleared now, that same keypress would read as a
    // second Escape and end select mode, taking the toolbar that should get the focus back with it.
    // A task (not a microtask, which a browser runs between the two listeners) clears the flag once
    // the dispatch is over — whichever element the Escape was aimed at, inside the dialog or not.
    onHide: () => {
      setTimeout(() => {
        confirmOpen = false
      })
    },
  })
}

function shareSelected() {
  const batch = new Set(selection.capture())
  void sharer.share(items.value.filter((i) => batch.has(i.uid)))
}

// While selecting, the shell's dock shows this toolbar and nothing else.
const SelectionDock = defineComponent({
  name: 'LibrarySelectionDock',
  setup: () => () =>
    h(LibrarySelectionToolbar, {
      count: selection.count.value,
      canShare: sharer.supported,
      sharing: sharer.pending.value,
      deleting: bulkDelete.pending.value,
      onShare: shareSelected,
      onDelete: deleteSelected,
    }),
})
useDockOverride(SelectionDock, () => selecting.value)

// One section per Zurich calendar month, in the list's own (takenAt) order, and in it one group per
// Zurich day (decision 13). Each group reports a tile index within its day; `start` maps it back
// onto the whole loaded list the viewer walks, and `index` counts days across the whole list so the
// frame cycle runs on over a month boundary.
const sections = computed(() => {
  let start = 0
  let dayIndex = 0
  return groupByMonth(items.value).map((section) => ({
    ...section,
    days: groupByDay(section.items).map((day) => {
      const withStart = { ...day, start, index: dayIndex++ }
      start += day.items.length
      return withStart
    }),
  }))
})
// A day heading's count is the server's for the whole day; while the buckets are pending or failed
// the cell stays empty rather than showing how many of the day happen to be loaded.
const dayCounts = computed(() => {
  if (!dayBuckets.isSuccess.value) return null
  return new Map((dayBuckets.data.value ?? []).map((b) => [b.key, b.count]))
})
function dayCount(key: string): number | null {
  if (key === UNDATED_KEY) return null
  return dayCounts.value?.get(key) ?? null
}

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
// Zurich day, and that day's heading is scrolled into view (decision 13). A later jump supersedes an earlier
// one (the token), so only the latest tap ever scrolls.
const root = ref<HTMLElement | null>(null)
let jumpToken = 0
// The jump lands a day's heading at the TOP of what is visible: under the shell's fixed chrome and
// the sticky month header. The header's height is measured (it depends on the font and the label),
// carried on the view as a variable, and the heading's scroll margin adds the two.
const monthHeaderHeight = ref(0)
const TILE_SCROLL_MARGIN = 'calc(var(--chrome-height, 0px) + var(--month-header-height, 0px))'
onUnmounted(() => {
  jumpToken += 1
})
// Kept current while All shows (a font swap or a rotation changes it): the jump's landing row and
// the date pill both sit under it. The border box, since the header's padding is part of what sticks.
let headerObserver: ResizeObserver | null = null
let observedHeader: HTMLElement | null = null
function firstMonthHeader(): HTMLElement | null {
  return root.value?.querySelector<HTMLElement>('[data-test="month-header"]') ?? null
}
function measureMonthHeader() {
  const header = firstMonthHeader()
  if (header) monthHeaderHeight.value = header.getBoundingClientRect().height
}
function unobserveMonthHeader() {
  headerObserver?.disconnect()
  headerObserver = null
  observedHeader = null
}
// `root` is watched too: an immediate run in setup sees no DOM yet, and a list already cached at
// mount changes neither the view nor the sections afterwards, so the ref's arrival is the only cue.
watch(
  () => [view.value, sections.value, root.value] as const,
  () => {
    const header = view.value === 'all' ? firstMonthHeader() : null
    if (header === observedHeader) return
    unobserveMonthHeader()
    if (!header) return
    observedHeader = header
    monthHeaderHeight.value = header.getBoundingClientRect().height
    if (typeof ResizeObserver === 'undefined') return
    headerObserver = new ResizeObserver(() => {
      monthHeaderHeight.value = header.getBoundingClientRect().height
    })
    headerObserver.observe(header)
  },
  { flush: 'post', immediate: true },
)
onUnmounted(unobserveMonthHeader)

// The date while scrolling (decision 13): a pill names the day whose heading has reached the line
// under the chrome and the month header, shows on scroll and hides one second after the last
// scroll event; a tap opens Months at that day's month.
const PILL_HIDE_MS = 1000
const pillDay = ref<string | null>(null)
let pillTimer: ReturnType<typeof setTimeout> | null = null
function chromeHeight(): number {
  const el = root.value
  if (!el) return 0
  return parseFloat(getComputedStyle(el).getPropertyValue('--chrome-height')) || 0
}
function hidePill() {
  if (pillTimer !== null) clearTimeout(pillTimer)
  pillTimer = null
  pillDay.value = null
}
function onScroll() {
  if (view.value !== 'all' || !root.value) {
    hidePill()
    return
  }
  const headings = [...root.value.querySelectorAll<HTMLElement>('[data-day]')].map((el) => ({
    key: el.dataset.day ?? '',
    top: el.getBoundingClientRect().top,
  }))
  pillDay.value = topDay(headings, chromeHeight() + monthHeaderHeight.value)
  if (pillTimer !== null) clearTimeout(pillTimer)
  pillTimer = setTimeout(hidePill, PILL_HIDE_MS)
}
onMounted(() => document.addEventListener('scroll', onScroll, { passive: true }))
onUnmounted(() => {
  document.removeEventListener('scroll', onScroll)
  hidePill()
})
watch(view, (v) => {
  if (v !== 'all') hidePill()
})

// The pill's way up: Months opens and, once its day buckets have loaded and rendered, that month's
// section is scrolled to the top under the chrome; a month Months does not show lands at the top.
// A later view or filter change, or a newer tap, cancels a landing still waiting.
let monthJumpToken = 0
watch(view, (v) => {
  if (v !== 'months') monthJumpToken++
})
watch(filter, () => {
  monthJumpToken++
})
onUnmounted(() => {
  monthJumpToken++
})
function bucketsSettled(token: number): Promise<boolean> {
  const done = () => dayBuckets.isSuccess.value || dayBuckets.isError.value || token !== monthJumpToken
  return new Promise((resolve) => {
    if (done()) {
      resolve(dayBuckets.isSuccess.value && token === monthJumpToken)
      return
    }
    const stop = watch(done, (d) => {
      if (!d) return
      stop()
      resolve(dayBuckets.isSuccess.value && token === monthJumpToken)
    })
  })
}
async function openMonthFromPill() {
  const day = pillDay.value
  if (!day) return
  hidePill()
  const month = day.slice(0, 7)
  setView('months')
  const token = ++monthJumpToken
  // A failed load drops the target: Months shows its own error and retry.
  if (!(await bucketsSettled(token))) return
  for (let frame = 0; frame < 120 && token === monthJumpToken; frame++) {
    await nextTick()
    const mosaic = root.value?.querySelector<HTMLElement>('[data-test="day-mosaic"]')
    const target = [...(mosaic?.querySelectorAll<HTMLElement>('[data-test="mosaic-section"]') ?? [])].find(
      (el) => el.dataset.month === month,
    )
    if (target) {
      target.style.scrollMarginTop = 'var(--chrome-height, 0px)'
      target.scrollIntoView({ block: 'start' })
      return
    }
    if (mosaic) break
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
  }
  if (token === monthJumpToken) window.scrollTo({ top: 0 })
}
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
// The day's heading exists once the list refs caught up and the view rendered, a few frames on.
async function headingFor(day: string, token: number): Promise<HTMLElement | null> {
  for (let frame = 0; frame < 120 && token === jumpToken; frame++) {
    await nextTick()
    const headings = root.value?.querySelectorAll<HTMLElement>('[data-day]') ?? []
    const heading = [...headings].find((el) => el.dataset.day === day)
    if (heading) return heading
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
    const uid = await scrollToDay(day, pager, undefined, () => token === jumpToken)
    if (!uid || token !== jumpToken) return
    // Decision 13: the landing row is the day's heading, not its first tile.
    const heading = await headingFor(day, token)
    if (!heading || token !== jumpToken) return
    measureMonthHeader()
    await nextTick()
    if (token === jumpToken) heading.scrollIntoView({ block: 'start' })
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
      if (!(await deleteAttachment(api, uid))) {
        toast.add({ severity: 'error', summary: 'Delete failed', life: 4000 })
        return
      }
      await queryClient.invalidateQueries({ queryKey: ['attachments'] })
    },
  })
}
</script>

<template>
  <section
    ref="root"
    class="mx-auto flex max-w-6xl flex-col gap-4 p-4"
    :style="{ '--month-header-height': `${monthHeaderHeight}px` }"
    data-test="view-resources"
  >
    <LibraryHeader
      v-model:filter="filter"
      :selecting="selecting"
      :can-select="view === 'all'"
      :all-selected="allSelected"
      @select="enterSelect"
      @select-all="selection.selectAllLoaded"
      @select-close="leaveSelect"
    />
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
      <div
        v-else-if="bucketView.empty"
        class="flex flex-col items-start gap-2 text-sm text-muted-color"
        data-test="resources-empty"
      >
        <p>{{ emptyState.message }}</p>
        <Button
          v-if="emptyState.filtered"
          label="Clear filter"
          icon="pi pi-filter-slash"
          size="small"
          outlined
          data-test="resources-empty-clear"
          @click="clearFilter"
        />
      </div>
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
    <div
      v-else-if="!items.length"
      class="flex flex-col items-start gap-2 text-sm text-muted-color"
      data-test="resources-empty"
    >
      <p>{{ emptyState.message }}</p>
      <Button
        v-if="emptyState.filtered"
        label="Clear filter"
        icon="pi pi-filter-slash"
        size="small"
        outlined
        data-test="resources-empty-clear"
        @click="clearFilter"
      />
    </div>
    <template v-else>
      <!-- The month header sticks under the shell's chrome, whose height the shell measures. -->
      <div v-for="section in sections" :key="section.key" class="flex flex-col gap-2">
        <h2
          class="sticky top-[var(--chrome-height,0px)] z-10 -mx-4 bg-surface-50/90 px-4 py-2 text-lg font-semibold backdrop-blur-sm dark:bg-surface-950/90"
          data-test="month-header"
        >
          {{ section.label }}
        </h2>
        <DayGroup
          v-for="day in section.days"
          :key="day.key"
          :day="day.key"
          :items="day.items"
          :day-index="day.index"
          :count="dayCount(day.key)"
          :heading-scroll-margin="TILE_SCROLL_MARGIN"
          :selecting="selecting"
          :selected="selected"
          @select="selection.toggle"
          @open="openAt(day.start + $event)"
          @retry="onTileRetry"
          @print-retry="printMaster.retry($event)"
        />
      </div>
      <!-- Top centre under the header, clear of the header's buttons; above the sticky month header. -->
      <Transition name="date-pill">
        <button
          v-if="pillDay"
          type="button"
          class="glass fixed left-1/2 z-20 -translate-x-1/2 rounded-full px-4 py-1.5 text-sm font-semibold shadow-md"
          :style="{ top: 'calc(var(--chrome-height, 0px) + var(--month-header-height, 0px) + 8px)' }"
          data-test="date-pill"
          @click="openMonthFromPill"
        >
          {{ dayPillLabel(pillDay) }}
        </button>
      </Transition>
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

<style scoped>
.date-pill-leave-active {
  transition: opacity 0.3s ease;
}
.date-pill-leave-to {
  opacity: 0;
}
</style>
