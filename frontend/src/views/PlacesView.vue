<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import Skeleton from 'primevue/skeleton'
import {
  computed,
  onBeforeUnmount,
  onMounted,
  onUnmounted,
  ref,
  shallowRef,
  useTemplateRef,
} from 'vue'
import { useRouter } from 'vue-router'

import ClusterDrawer from '@/components/places/ClusterDrawer.vue'
import InViewGrid from '@/components/places/InViewGrid.vue'
import { asyncMapComponent, mapLoadRung } from '@/components/places/asyncMap'
// Type only: a value import here would put the whole map chunk back on the route's critical path.
import type PlacesMap from '@/components/places/PlacesMap.vue'
import { useApi } from '@/composables/useApi'
import { useMediaLightbox } from '@/composables/useMediaLightbox'
import type { MapMarker } from '@/lib/places/geo'
import { memoPath, toLightboxItems } from '@/lib/places/lightbox'
import { loadPlacesMap } from '@/lib/places/loadMap'
import { viewerTarget } from '@/lib/places/order'
import { placesStatus } from '@/lib/places/state'

const api = useApi()
const router = useRouter()
const mapRef = useTemplateRef<InstanceType<typeof PlacesMap>>('map')

// The map is a megabyte of MapLibre and the only code the service worker does not precache, so
// the route may not block on it: the shell paints, the pane says it is loading, and a chunk that
// never arrives gets a way to ask again (a fresh component — see `asyncMapComponent`).
const mapFailures = ref(0)
function loadMapPane() {
  return loadPlacesMap().catch((error: unknown) => {
    mapFailures.value += 1
    throw error
  })
}
const recovery = { rung: () => mapLoadRung(mapFailures.value), onAct: recoverMap }
const mapComponent = shallowRef(asyncMapComponent(loadMapPane, recovery))
function recoverMap() {
  // The last rung: once a retry has failed too, the browser may be answering from its memory of
  // the first failure and no further attempt in this document can reach the network.
  if (mapLoadRung(mapFailures.value) === 'reload') {
    window.location.reload()
    return
  }
  mapComponent.value = asyncMapComponent(loadMapPane, recovery)
}

const { data, isPending, isError } = useQuery({
  queryKey: ['map', 'markers'],
  queryFn: async () => {
    const { data, error } = await api.GET('/api/map/markers')
    if (error) throw error
    return data
  },
})

const markers = computed<MapMarker[]>(() => data.value?.markers ?? [])
// Pending, failed and genuinely empty are three different things to tell a viewer, and counting
// markers cannot tell them apart — see `placesStatus`.
const status = computed(() =>
  placesStatus({
    isPending: isPending.value,
    isError: isError.value,
    markerCount: markers.value.length,
  }),
)
const byUid = computed(() => new Map(markers.value.map((m) => [m.uid, m])))

// What the map currently has in frame, and — when a cluster was tapped — the photos behind that
// one pin. ONE set: the drawer, the phone grid and the viewer all read it.
const visibleUids = ref<string[]>([])
const selection = ref<{ clusterId: number | null; uids: string[] } | null>(null)

// The one place a list of uids becomes photos: a cluster's leaves as MapLibre hands them out on
// tap, and the map's in-view report, are both lists of uids and nothing more.
function resolve(uids: readonly string[]): MapMarker[] {
  const lookup = byUid.value
  return uids.map((uid) => lookup.get(uid)).filter((m): m is MapMarker => !!m)
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
})
onBeforeUnmount(() => {
  media?.removeEventListener('change', syncPhone)
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
      <component
        :is="mapComponent"
        ref="map"
        :markers="markers"
        @cluster-open="onClusterOpen"
        @point-open="onPointOpen"
        @visible-change="visibleUids = $event"
      />
      <Skeleton
        v-if="status === 'pending'"
        class="!absolute inset-0 !h-full !w-full"
        data-test="places-loading"
      />
      <p
        v-if="status === 'empty'"
        class="pointer-events-none absolute inset-0 flex items-center justify-center p-6 text-center text-muted-color"
        data-test="places-empty"
      >
        No photo here knows where it was taken yet.
      </p>
      <p
        v-else-if="status === 'error'"
        class="pointer-events-none absolute inset-0 flex items-center justify-center p-6 text-center text-muted-color"
        data-test="places-error"
      >
        The map could not be loaded. Try again in a moment.
      </p>
    </div>

    <InViewGrid
      v-if="isPhone"
      class="min-h-0 flex-1 overflow-y-auto pb-[var(--dock-clearance)]"
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
