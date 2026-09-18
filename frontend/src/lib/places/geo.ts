// Pure geometry for the Places map: markers in, GeoJSON and boxes out. Kept out of the map
// component because a MapLibre canvas cannot run in happy-dom, while every rule that decides
// what the first view frames and which photos share a pin can — and must — be tested directly.

import type { FeatureCollection, Point } from 'geojson'

import type { components } from '@/api/schema'

export type MapMarker = components['schemas']['MapMarker']

/** What travels into the clustering source — and, on every `setData`, into MapLibre's worker. */
export interface MarkerProps {
  uid: string
  /** taken_at as epoch millis — a number survives the trip into MapLibre's worker as it is. */
  t: number
}

export type Bbox = [west: number, south: number, east: number, north: number]

/** ~10 cm. Two files from the same spot must land on ONE pin; two houses apart must not. */
const COORD_DECIMALS = 6

/**
 * The smallest box `fitBounds` is given. One pin (or a column of pins on one coordinate) has a
 * zero-width box, which has no scale to solve for — the first view would land at an arbitrary
 * zoom instead of on the pin.
 */
export const MIN_BBOX_SPAN = 0.01

export function coordKey(latitude: number, longitude: number): string {
  return `${latitude.toFixed(COORD_DECIMALS)},${longitude.toFixed(COORD_DECIMALS)}`
}

export function toFeatureCollection(
  markers: readonly MapMarker[],
): FeatureCollection<Point, MarkerProps> {
  return {
    type: 'FeatureCollection',
    features: markers.map((m) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [m.longitude, m.latitude] },
      properties: { uid: m.uid, t: Date.parse(m.taken_at) },
    })),
  }
}

/** The box every pin fits in, padded on any axis that collapsed. Null when there are no pins. */
export function bboxOf(markers: readonly MapMarker[]): Bbox | null {
  if (markers.length === 0) return null
  let west = Infinity
  let south = Infinity
  let east = -Infinity
  let north = -Infinity
  for (const m of markers) {
    west = Math.min(west, m.longitude)
    east = Math.max(east, m.longitude)
    south = Math.min(south, m.latitude)
    north = Math.max(north, m.latitude)
  }
  if (east - west < MIN_BBOX_SPAN) {
    const mid = (east + west) / 2
    west = mid - MIN_BBOX_SPAN / 2
    east = mid + MIN_BBOX_SPAN / 2
  }
  if (north - south < MIN_BBOX_SPAN) {
    const mid = (north + south) / 2
    south = mid - MIN_BBOX_SPAN / 2
    north = mid + MIN_BBOX_SPAN / 2
  }
  return [west, south, east, north]
}

export interface CoordinateGroup {
  key: string
  latitude: number
  longitude: number
  markers: MapMarker[]
}

/** Photos taken from one spot, in input order — they share a single pin at every zoom. */
export function groupByCoordinate(markers: readonly MapMarker[]): CoordinateGroup[] {
  const groups = new Map<string, CoordinateGroup>()
  for (const m of markers) {
    const key = coordKey(m.latitude, m.longitude)
    const group = groups.get(key)
    if (group) group.markers.push(m)
    else groups.set(key, { key, latitude: m.latitude, longitude: m.longitude, markers: [m] })
  }
  return [...groups.values()]
}

/** The breathing room the first view keeps around the outermost pins, per side. */
export const FIT_PADDING = 50
