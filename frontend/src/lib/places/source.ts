// Installing the clustering source and its probe layer on a map. It lives here, apart from the
// component, because it has to run MORE THAN ONCE: `setStyle` (the theme toggle) replaces the
// whole style, and MapLibre's diff drops every source and layer the new style does not declare —
// while `load` fires only for the map's first style. Re-running this after a style swap is what
// keeps the pins live, the cluster leaves findable and the phone grid in step with the map.

import type { GeoJSONSourceSpecification, LayerSpecification } from 'maplibre-gl'

import type { MarkerProps } from '@/lib/places/geo'
import type { FeatureCollection, Point } from 'geojson'

export const MARKERS_SOURCE = 'markers'
export const MARKERS_PROBE_LAYER = 'markers-probe'

/** The slice of the MapLibre map this needs — small enough to drive from a test without a canvas. */
export interface MarkerSourceHost {
  getSource(id: string): unknown
  getLayer(id: string): unknown
  addSource(id: string, spec: GeoJSONSourceSpecification): void
  addLayer(spec: LayerSpecification): void
}

/**
 * Put the markers source and its probe layer on the map unless they are already there.
 * Returns true when it installed them, i.e. when the caller has to re-sync the pins.
 */
export function installMarkerSource(
  host: MarkerSourceHost,
  data: FeatureCollection<Point, MarkerProps>,
): boolean {
  let installed = false
  if (!host.getSource(MARKERS_SOURCE)) {
    host.addSource(MARKERS_SOURCE, {
      type: 'geojson',
      data,
      cluster: true,
      clusterRadius: 40,
      clusterMaxZoom: 17,
      // The lead photo of a cluster, carried by the cluster itself: the alternative is a
      // getClusterLeaves round trip per pin on every pan.
      clusterProperties: { uid: [['coalesce', ['accumulated'], ['get', 'uid']], ['get', 'uid']] },
    })
    installed = true
  }
  if (!host.getLayer(MARKERS_PROBE_LAYER)) {
    // Nothing draws this layer (opacity 0) — it exists because MapLibre only loads a source's
    // tiles, and therefore only clusters, for a layer that uses it. The pins are DOM markers.
    host.addLayer({
      id: MARKERS_PROBE_LAYER,
      type: 'circle',
      source: MARKERS_SOURCE,
      paint: { 'circle-radius': 1, 'circle-opacity': 0 },
    })
    installed = true
  }
  return installed
}
