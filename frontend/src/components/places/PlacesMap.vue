<script setup lang="ts">
// MapLibre v6 fetches its worker from a URL next to its own module (import.meta.url), which after
// bundling is a path Vite never emitted. Handing it the bundled worker's real URL is what makes the
// map work in a built SPA — and `worker.rollupOptions` names that file `map-worker-*` so the
// service worker skips it exactly like the map chunk.
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import {
  addProtocol,
  AttributionControl,
  type GeoJSONSource,
  Map as MapLibreMap,
  Marker,
  NavigationControl,
  setWorkerUrl,
} from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { Protocol } from 'pmtiles'
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import { useApi } from '@/composables/useApi'
import { useDarkMode } from '@/composables/useDarkMode'
import { bboxOf, FIT_PADDING, type MapMarker, toFeatureCollection } from '@/lib/places/geo'
import { blurhashToDataUrl } from '@/lib/media'
import { badgeText, type PinSpec, pinsFor, type SourceFeature, syncMarkers } from '@/lib/places/markers'
import { createPinElement } from '@/lib/places/pin'
import { installMarkerSource, MARKERS_SOURCE } from '@/lib/places/source'
import { ATTRIBUTION, buildStyle, type MapManifest } from '@/lib/places/style'

const props = defineProps<{
  markers: MapMarker[]
}>()
const emit = defineEmits<{
  'cluster-open': [payload: { clusterId: number | null; uids: string[] }]
  'point-open': [uid: string]
  'visible-change': [uids: string[]]
}>()

const SOURCE = MARKERS_SOURCE
const EMPTY_MANIFEST: MapManifest = {
  build: null,
  assets_commit: null,
  planet: null,
  detail: null,
  uncovered: null,
}

// The pmtiles protocol is a MapLibre global, not a per-map one: registering it twice throws.
let protocolRegistered = false
function registerProtocol() {
  if (protocolRegistered) return
  setWorkerUrl(workerUrl)
  addProtocol('pmtiles', new Protocol().tile)
  protocolRegistered = true
}

const api = useApi()
const { isDark } = useDarkMode()
const container = ref<HTMLElement | null>(null)
const map = shallowRef<MapLibreMap | null>(null)
/** Milliseconds the last marker sync took — read off the DOM by the performance check. */
const syncMs = ref(0)

const byUid = new Map<string, MapMarker>()
const pins = new Map<string, { pin: PinSpec; marker: Marker; element: HTMLElement }>()
let fitted = false

function source(): GeoJSONSource | undefined {
  return map.value?.getSource(SOURCE) as GeoJSONSource | undefined
}

/**
 * The map's one data entry point: the `markers` prop, handed to the clustering source so MapLibre
 * re-clusters them — a layer `filter` would hide points without re-clustering (mapbox-gl-js#2476).
 */
function feedSource(markers: readonly MapMarker[]) {
  source()?.setData(toFeatureCollection(markers))
  sync()
}
defineExpose({ zoomToCluster })

function paint(element: HTMLElement, pin: PinSpec) {
  const lead = byUid.get(pin.uid)
  // By class, not by tag: the badge and the thumbnail sit inside a wrapper element that is itself
  // a span (the landing animation's home), which `querySelector('span')` would find first.
  const image = element.querySelector<HTMLImageElement>('.places-pin__thumb')
  const badge = element.querySelector<HTMLElement>('.places-pin__badge')
  if (image && lead) {
    const placeholder = blurhashToDataUrl(lead.blurhash)
    if (placeholder) image.style.backgroundImage = `url(${placeholder})`
    image.src = lead.mime.startsWith('video/') ? lead.posterUrl : lead.thumbnailUrl
  }
  if (badge) {
    const text = badgeText(pin.count)
    badge.textContent = text ?? ''
    badge.style.display = text ? '' : 'none'
  }
  element.setAttribute(
    'aria-label',
    pin.count > 1 ? `${pin.count} photos taken here` : 'One photo taken here',
  )
}

function elementFor(pin: PinSpec): HTMLElement {
  const element = createPinElement()
  element.dataset.key = pin.key
  element.addEventListener('click', (event) => {
    event.stopPropagation()
    void openPin(element.dataset.key ?? '')
  })
  return element
}

/** A tap on a pin: one photo opens the viewer, anything else opens the cluster. */
async function openPin(key: string) {
  const entry = pins.get(key)
  if (!entry) return
  const { pin } = entry
  if (pin.clusterId == null) {
    if (pin.count === 1) emit('point-open', pin.uid)
    else emit('cluster-open', { clusterId: null, uids: pin.uids })
    return
  }
  const leaves = await source()?.getClusterLeaves(pin.clusterId, pin.count, 0)
  const uids = (leaves ?? [])
    .map((leaf) => (leaf.properties as { uid?: string }).uid)
    .filter((uid): uid is string => !!uid)
  emit('cluster-open', { clusterId: pin.clusterId, uids })
}

async function zoomToCluster(clusterId: number) {
  const entry = [...pins.values()].find((p) => p.pin.clusterId === clusterId)
  const zoom = await source()?.getClusterExpansionZoom(clusterId)
  if (zoom == null || !entry) return
  map.value?.easeTo({ center: [entry.pin.lng, entry.pin.lat], zoom })
}

/** Repaint the DOM markers for what MapLibre currently holds, and report what is in view. */
function sync() {
  const instance = map.value
  if (!instance || !instance.getSource(SOURCE)) return
  const started = performance.now()
  const centre = instance.getCenter()
  const features = instance.querySourceFeatures(SOURCE) as unknown as SourceFeature[]
  const desired = pinsFor(features, { center: [centre.lng, centre.lat] })
  const current = new Map([...pins.entries()].map(([key, entry]) => [key, entry.pin]))
  const diff = syncMarkers(current, desired)

  for (const key of diff.remove) {
    pins.get(key)?.marker.remove()
    pins.delete(key)
  }
  for (const pin of diff.add) {
    const element = elementFor(pin)
    paint(element, pin)
    const marker = new Marker({ element }).setLngLat([pin.lng, pin.lat]).addTo(instance)
    pins.set(pin.key, { pin, marker, element })
  }
  for (const pin of diff.update) {
    const entry = pins.get(pin.key)
    if (!entry) continue
    entry.pin = pin
    entry.marker.setLngLat([pin.lng, pin.lat])
    paint(entry.element, pin)
  }

  syncMs.value = Math.round(performance.now() - started)
  const bounds = instance.getBounds()
  emit(
    'visible-change',
    props.markers.filter((m) => bounds.contains([m.longitude, m.latitude])).map((m) => m.uid),
  )
}

/** The opening view: the pins, not the world. Nothing to show means the world at z1. */
function fitToMarkers() {
  const instance = map.value
  if (!instance) return
  const box = bboxOf(props.markers)
  if (!box) {
    instance.jumpTo({ center: [0, 20], zoom: 1 })
    return
  }
  const padding = {
    top: FIT_PADDING,
    right: FIT_PADDING,
    bottom: FIT_PADDING,
    left: FIT_PADDING,
  }
  instance.fitBounds(box, { padding, maxZoom: 15, animate: false })
}

async function loadManifest(): Promise<MapManifest> {
  // A basemap that cannot be fetched is not a reason to withhold the pins: the style falls back
  // to a plain background and the markers, drawer and grid work exactly as they would.
  try {
    const { data } = await api.GET('/api/map/manifest')
    return data ?? EMPTY_MANIFEST
  } catch {
    return EMPTY_MANIFEST
  }
}

onMounted(async () => {
  registerProtocol()
  const manifest = await loadManifest()
  if (!container.value) return
  const instance = new MapLibreMap({
    container: container.value,
    style: buildStyle(manifest, isDark.value ? 'dark' : 'light'),
    center: [0, 20],
    zoom: 1,
    attributionControl: false,
  })
  map.value = instance
  instance.addControl(new AttributionControl({ compact: true, customAttribution: ATTRIBUTION }))
  instance.addControl(new NavigationControl({ showCompass: false }), 'top-right')

  instance.on('load', () => {
    installMarkerSource(instance, toFeatureCollection(props.markers))
    fitToMarkers()
    fitted = props.markers.length > 0
    sync()
  })
  // Every style the map takes on needs the markers put back: `setStyle` (the theme toggle) hands
  // MapLibre a style that knows nothing of them, its diff drops the source and the probe layer,
  // and `load` never fires again. The installer no-ops while they are there, so this is the one
  // place that notices they are gone, and it re-syncs the pins onto the source it just put back.
  instance.on('styledata', () => {
    if (installMarkerSource(instance, toFeatureCollection(props.markers))) sync()
  })
  instance.on('moveend', sync)
  instance.on('idle', sync)
})

watch(
  () => props.markers,
  (markers) => {
    byUid.clear()
    for (const marker of markers) byUid.set(marker.uid, marker)
    if (!map.value?.getSource(SOURCE)) return
    feedSource(markers)
    if (!fitted && markers.length) {
      fitToMarkers()
      fitted = true
    }
  },
  { immediate: true },
)

// The basemap flavour follows the app's theme. The camera survives the swap on its own; the
// markers are put back by the `styledata` handler above.
watch(isDark, async (dark) => {
  const instance = map.value
  if (!instance) return
  instance.setStyle(buildStyle(await loadManifest(), dark ? 'dark' : 'light'))
})

onBeforeUnmount(() => {
  for (const entry of pins.values()) entry.marker.remove()
  pins.clear()
  map.value?.remove()
  map.value = null
})
</script>

<template>
  <div
    ref="container"
    class="h-full w-full"
    data-test="places-map"
    :data-sync-ms="syncMs"
    role="application"
    aria-label="Map of where the photos were taken"
  />
</template>

<style>
/* The pin rules moved to `lib/places/pin.css`, next to the builder that creates those elements —
   what may and may not be styled on a MapLibre marker root is a rule that belongs with it. */
</style>

