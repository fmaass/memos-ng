// The MapLibre style, assembled here rather than shipped as a file: the archives are named by
// their build (an immutable URL per extraction), so the style can only be written once the
// manifest is known. Every URL it emits is same-origin — the whole point of self-hosting the
// basemap is that opening the map tells no one else where the household has been.

import { type Flavor, layers, namedFlavor } from '@protomaps/basemaps'
import type { LayerSpecification, SourceSpecification, StyleSpecification } from 'maplibre-gl'

import type { components } from '@/api/schema'

export type MapManifest = components['schemas']['MapManifest']
export type MapFlavor = 'dark' | 'light'

/** Plain text, not a link: the style must contain no URL the browser could leave this origin on. */
export const ATTRIBUTION = '© OpenStreetMap contributors © Protomaps'

export const GLYPHS_URL = '/api/map/assets/fonts/{fontstack}/{range}.pbf'

/**
 * The asset URL a client should ask for, given the assets revision the manifest reported.
 *
 * The archives carry their version in the file NAME; the glyphs and sprites cannot — the style
 * references them by fixed path and `fetch-assets.sh` overwrites them in place when its pin moves.
 * The route still answers them with a week of `immutable`, so the query is what makes that
 * truthful: a bumped pin is a URL nobody has cached. With no revision recorded there is nothing to
 * version against, and a made-up value would cache just as wrongly, so the URL is left bare.
 */
export function versionedAssetUrl(url: string, assetsCommit: string | null | undefined): string {
  return assetsCommit ? `${url}?v=${assetsCommit.slice(0, 8)}` : url
}

/**
 * Where the world archive stops and the region archive takes over. The planet archive is cut at
 * z7 and the detail archive covers z0–15, so z8 is the seam: below it the world, above it the
 * places the pins are.
 */
export const DETAIL_MIN_ZOOM = 8

function sourceFor(url: string): SourceSpecification {
  // `pmtiles://` is handled by the protocol the map registers; MapLibre never fetches this URL
  // itself, it asks the protocol, which range-reads the archive.
  return { type: 'vector', url: `pmtiles://${url}` }
}

/** The world set: everything that draws below the seam, capped there. */
function planetLayers(list: LayerSpecification[]): LayerSpecification[] {
  return list
    .filter((layer) => (layer.minzoom ?? 0) < DETAIL_MIN_ZOOM)
    .map((layer) => ({
      ...layer,
      id: `planet:${layer.id}`,
      maxzoom: Math.min(layer.maxzoom ?? DETAIL_MIN_ZOOM, DETAIL_MIN_ZOOM),
    }))
}

/** The region set: everything that draws above the seam, floored there. */
function detailLayers(list: LayerSpecification[]): LayerSpecification[] {
  return list
    .filter((layer) => (layer.maxzoom ?? Infinity) > DETAIL_MIN_ZOOM)
    .map((layer) => ({
      ...layer,
      id: `detail:${layer.id}`,
      minzoom: Math.max(layer.minzoom ?? 0, DETAIL_MIN_ZOOM),
    }))
}

function background(flavor: Flavor): LayerSpecification {
  return { id: 'background', type: 'background', paint: { 'background-color': flavor.background } }
}

/**
 * The style for one flavor against whatever archives exist.
 *
 * With no detail archive the planet layers keep NO maxzoom: an overzoomed basemap beats a blank
 * one past z8. With no archive at all the style is the background colour alone — the pins, the
 * drawer and the grid are the feature, and a missing basemap may not take them down with it.
 */
export function buildStyle(manifest: MapManifest, flavorName: MapFlavor): StyleSpecification {
  const flavor = namedFlavor(flavorName)
  const sources: Record<string, SourceSpecification> = {}
  const styleLayers: LayerSpecification[] = [background(flavor)]

  if (manifest.planet) {
    sources.planet = sourceFor(manifest.planet.url)
    const planet = layers('planet', flavor, { lang: 'en' }).filter((l) => l.type !== 'background')
    styleLayers.push(...(manifest.detail ? planetLayers(planet) : planet))
  }
  if (manifest.detail) {
    sources.detail = sourceFor(manifest.detail.url)
    const detail = layers('detail', flavor, { lang: 'en' }).filter((l) => l.type !== 'background')
    styleLayers.push(...detailLayers(detail))
  }

  return {
    version: 8,
    glyphs: versionedAssetUrl(GLYPHS_URL, manifest.assets_commit),
    sprite: versionedAssetUrl(`/api/map/assets/sprites/v4/${flavorName}`, manifest.assets_commit),
    sources,
    layers: styleLayers,
  }
}
