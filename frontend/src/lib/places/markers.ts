// The DOM-marker layer's decisions, as pure functions: which pins the map should be showing for
// the features MapLibre currently has, and what changed since the last frame. The component does
// the DOM; this decides. (Decision 3 of the plan: DOM markers, not symbol layers — if that ever
// flips, this module and its tests survive the rewrite.)

import type { Point } from 'geojson'

import { coordKey } from '@/lib/places/geo'

/** A feature as `querySourceFeatures` hands it back: a cluster, or one located attachment. */
export interface SourceFeature {
  geometry: Point
  properties: {
    cluster?: boolean
    cluster_id?: number
    point_count?: number
    /** The cluster's lead photo (clusterProperties), or the point's own attachment. */
    uid?: string
    t?: number
  }
}

/** One pin the map should be showing. */
export interface PinSpec {
  key: string
  lng: number
  lat: number
  /** Photos behind this pin: a cluster's point_count, or how many share the coordinate. */
  count: number
  /** The photo whose thumbnail the pin wears. */
  uid: string
  /** MapLibre's cluster id — null for a pin that is already a single coordinate (no zoom-in). */
  clusterId: number | null
  /** The attachments on this coordinate. Empty for a cluster: its leaves are fetched on tap. */
  uids: string[]
}

/**
 * Above `clusterMaxZoom` MapLibre stops clustering, so a dense city block is one DOM node per
 * photo. 500 is the ceiling the plan sets; the ones nearest the viewport centre win, and the
 * drawer and the grid still list every photo in view.
 */
export const MAX_DOM_MARKERS = 500

/** Badge text, or null when the pin carries no badge. */
export function badgeText(count: number): string | null {
  if (count <= 1) return null
  return count > 99 ? '99+' : String(count)
}

export function pinsFor(
  features: readonly SourceFeature[],
  options: { center: [number, number]; cap?: number },
): PinSpec[] {
  const pins: PinSpec[] = []
  const byCoordinate = new Map<string, PinSpec>()
  // querySourceFeatures answers from every loaded tile, so a cluster on a tile boundary and a
  // point still cached at another zoom both come back more than once.
  const seenClusters = new Set<number>()
  const seenUids = new Set<string>()

  for (const feature of features) {
    const [lng, lat] = feature.geometry.coordinates
    const props = feature.properties
    if (props.cluster && props.cluster_id != null) {
      if (seenClusters.has(props.cluster_id)) continue
      seenClusters.add(props.cluster_id)
      pins.push({
        key: `c:${props.cluster_id}`,
        lng,
        lat,
        count: props.point_count ?? 0,
        uid: props.uid ?? '',
        clusterId: props.cluster_id,
        uids: [],
      })
      continue
    }
    if (!props.uid || seenUids.has(props.uid)) continue
    seenUids.add(props.uid)
    const key = `p:${coordKey(lat, lng)}`
    const existing = byCoordinate.get(key)
    if (existing) {
      existing.count += 1
      existing.uids.push(props.uid)
      continue
    }
    const pin: PinSpec = { key, lng, lat, count: 1, uid: props.uid, clusterId: null, uids: [props.uid] }
    byCoordinate.set(key, pin)
    pins.push(pin)
  }

  const cap = options.cap ?? MAX_DOM_MARKERS
  if (pins.length <= cap) return pins
  const [cx, cy] = options.center
  return [...pins]
    .sort((a, b) => {
      const da = (a.lng - cx) ** 2 + (a.lat - cy) ** 2
      const db = (b.lng - cx) ** 2 + (b.lat - cy) ** 2
      return da - db || a.key.localeCompare(b.key)
    })
    .slice(0, cap)
}

export interface MarkerDiff {
  add: PinSpec[]
  update: PinSpec[]
  remove: string[]
}

/**
 * The photos behind two pins, compared in order. A deletion answered by an arrival at the same
 * coordinate leaves the lead, the count and the place untouched while the members move — and the
 * members are what a tap hands the drawer, so an unrefreshed pin opens on a photo that is gone.
 * Order counts because the drawer lists the photos in it, and `pinsFor` builds it deterministically
 * from the order the source was fed.
 */
function sameUids(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((uid, i) => uid === b[i])
}

/** What to add, repaint and drop so the map shows `desired` — the repaint is the frame budget. */
export function syncMarkers(current: ReadonlyMap<string, PinSpec>, desired: readonly PinSpec[]): MarkerDiff {
  const add: PinSpec[] = []
  const update: PinSpec[] = []
  const wanted = new Set<string>()

  for (const pin of desired) {
    wanted.add(pin.key)
    const existing = current.get(pin.key)
    if (!existing) add.push(pin)
    else if (
      existing.count !== pin.count ||
      existing.uid !== pin.uid ||
      existing.lng !== pin.lng ||
      existing.lat !== pin.lat ||
      !sameUids(existing.uids, pin.uids)
    )
      update.push(pin)
  }

  const remove = [...current.keys()].filter((key) => !wanted.has(key))
  return { add, update, remove }
}
