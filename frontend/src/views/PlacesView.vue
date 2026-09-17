<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import Skeleton from 'primevue/skeleton'
import { computed, onBeforeUnmount, onMounted, onUnmounted, ref, useTemplateRef, watch } from 'vue'
import { useRouter } from 'vue-router'

import ClusterDrawer from '@/components/places/ClusterDrawer.vue'
import InViewGrid from '@/components/places/InViewGrid.vue'
import PlacesMap from '@/components/places/PlacesMap.vue'
import TimelineBar from '@/components/places/TimelineBar.vue'
import { useApi } from '@/composables/useApi'
import { useMediaLightbox } from '@/composables/useMediaLightbox'
import type { MapMarker } from '@/lib/places/geo'
import { memoPath, toLightboxItems } from '@/lib/places/lightbox'
import { viewerTarget } from '@/lib/places/order'
import {
  applyPlayhead,
  buildHistogram,
  initialState,
  reduce,
  type TimelineAction,
  type TimelineState,
} from '@/lib/places/timeline'
import { createTicker, timelineKeyAction } from '@/lib/places/timelineBar'

const api = useApi()
const router = useRouter()
const mapRef = useTemplateRef<InstanceType<typeof PlacesMap>>('map')

const { data, isPending } = useQuery({
  queryKey: ['map', 'markers'],
  queryFn: async () => {
    const { data, error } = await api.GET('/api/map/markers')
    if (error) throw error
    return data
  },
})

const markers = computed<MapMarker[]>(() => data.value?.markers ?? [])
const byUid = computed(() => new Map(markers.value.map((m) => [m.uid, m])))

// The timeline is a FILTER, exactly like the map's viewport: one playhead decides which photos
// exist for the map, the drawer and the grid. The reducer owns every transition (lib/places/
// timeline.ts) — this view only drives it with a clock, the keyboard and the bar.
const histogram = computed(() => buildHistogram(markers.value))
const playhead = ref<TimelineState>(initialState(histogram.value))
const previous = ref<TimelineState | null>(null)
watch(histogram, (next) => {
  previous.value = null
  playhead.value = initialState(next)
})

const view = computed(() =>
  applyPlayhead(markers.value, playhead.value, {
    previous: previous.value,
    histogram: histogram.value,
  }),
)
const shownUids = computed(() => new Set(view.value.markers.map((m) => m.uid)))

function dispatch(action: TimelineAction) {
  const next = reduce(playhead.value, action)
  if (next === playhead.value) return
  previous.value = playhead.value
  playhead.value = next
}

// One `setData` per tick, with the pins that arrived in it — a MapLibre layer filter would hide
// points without re-clustering them, and the camera never moves: the pins land, the view holds.
watch(view, (current) => mapRef.value?.setVisibleMarkers(current.markers, current.landing), {
  flush: 'post',
})

// Speed advances BUCKETS, never the clock: one tick is one `setData` at 1×, 2× and 4× alike.
const ticker = createTicker(() => dispatch({ type: 'tick' }))
watch(
  () => playhead.value.playing,
  (playing) => (playing ? ticker.start() : ticker.stop()),
)

function onKeydown(event: KeyboardEvent) {
  const action = timelineKeyAction(event.key, playhead.value, event.target)
  if (!action) return
  event.preventDefault() // space would otherwise scroll the page under the map
  dispatch(action)
}

// What the map currently has in frame, and — when a cluster was tapped — the photos behind that
// one pin. ONE filtered set: the drawer, the phone grid and the viewer all read it, which is the
// seam the timeline (Phase 4) narrows further.
const visibleUids = ref<string[]>([])
const selection = ref<{ clusterId: number | null; uids: string[] } | null>(null)

// The one place a list of uids becomes photos — and the one place the playhead is applied to them.
// A cluster's leaves come from tiles MapLibre clustered before the playhead moved, and the map's
// in-view report is a list of uids too: both must be read through the timeline's filter.
function resolve(uids: readonly string[]): MapMarker[] {
  const lookup = byUid.value
  const shown = shownUids.value
  return uids
    .map((uid) => lookup.get(uid))
    .filter((m): m is MapMarker => !!m && shown.has(m.uid))
}

const inView = computed(() => resolve(visibleUids.value))
const selected = computed(() => (selection.value ? resolve(selection.value.uids) : []))
const gridMarkers = computed(() => (selection.value ? selected.value : inView.value))

const lightbox = useMediaLightbox()
onUnmounted(() => lightbox.destroy())

function openViewer(list: MapMarker[], index: number) {
  lightbox.open(toLightboxItems(list), index, {
    onShowInMemo: (memoUid) => {
      void router.push(memoPath({ memo_uid: memoUid }))
    },
  })
}

/** A pin or a grid tile names the photo it shows; the viewer opens on the order it is shown in. */
function openByUid(markers: MapMarker[], uid: string) {
  const target = viewerTarget(markers, uid)
  openViewer(target.list, target.index)
}

function onPointOpen(uid: string) {
  openByUid(inView.value, uid)
}

function onClusterOpen(payload: { clusterId: number | null; uids: string[] }) {
  selection.value = payload
}

function onZoomIn(clusterId: number) {
  selection.value = null
  void mapRef.value?.zoomToCluster(clusterId)
}

// The phone has no room for an overlay drawer beside the map, so there the same tap filters the
// grid instead (plan decision D10). One media query decides which of the two is on screen.
const isPhone = ref(false)
let media: MediaQueryList | null = null
function syncPhone() {
  isPhone.value = media?.matches ?? false
}
onMounted(() => {
  media = window.matchMedia('(max-width: 767px)')
  syncPhone()
  media.addEventListener('change', syncPhone)
  window.addEventListener('keydown', onKeydown)
  // The map is created with the full set; saying so once here makes this view the single source
  // of what the map shows, from the first frame rather than from the first tick.
  mapRef.value?.setVisibleMarkers(view.value.markers, view.value.landing)
})
onBeforeUnmount(() => {
  media?.removeEventListener('change', syncPhone)
  window.removeEventListener('keydown', onKeydown)
  ticker.stop()
})

const drawerOpen = computed({
  get: () => !isPhone.value && selection.value !== null,
  set: (open: boolean) => {
    if (!open) selection.value = null
  },
})
</script>

<template>
  <div class="flex h-[calc(100vh-3.5rem)] flex-col" data-test="view-places">
    <div class="relative h-[55vh] shrink-0 md:h-full md:shrink">
      <PlacesMap
        ref="map"
        :markers="markers"
        @cluster-open="onClusterOpen"
        @point-open="onPointOpen"
        @visible-change="visibleUids = $event"
      />
      <Skeleton
        v-if="isPending"
        class="!absolute inset-0 !h-full !w-full"
        data-test="places-loading"
      />
      <TimelineBar
        v-if="markers.length"
        class="absolute inset-x-2 bottom-2 z-10 md:inset-x-4 md:bottom-4"
        :histogram="histogram"
        :state="playhead"
        :phone="isPhone"
        @action="dispatch"
      />
      <p
        v-else-if="!markers.length"
        class="pointer-events-none absolute inset-0 flex items-center justify-center p-6 text-center text-muted-color"
        data-test="places-empty"
      >
        No photo here knows where it was taken yet.
      </p>
    </div>

    <InViewGrid
      v-if="isPhone"
      class="min-h-0 flex-1 overflow-y-auto"
      :markers="gridMarkers"
      :filtered="selection !== null"
      @open="openByUid(gridMarkers, $event)"
      @reset="selection = null"
    />
    <ClusterDrawer
      v-else
      v-model:visible="drawerOpen"
      :markers="selected"
      :cluster-id="selection?.clusterId ?? null"
      @open="openViewer(selected, $event)"
      @zoom-in="onZoomIn"
    />
  </div>
</template>
