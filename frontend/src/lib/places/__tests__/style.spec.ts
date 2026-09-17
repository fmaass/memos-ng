import { describe, expect, it } from 'vitest'

import { ATTRIBUTION, buildStyle, type MapManifest } from '@/lib/places/style'

const PLANET = { name: 'planet-20260916.pmtiles', url: '/api/map/tiles/planet-20260916.pmtiles', size: 1 }
const DETAIL = {
  name: 'detail-20260916-a7e95f1d.pmtiles',
  url: '/api/map/tiles/detail-20260916-a7e95f1d.pmtiles',
  size: 2,
}
const BOTH: MapManifest = { build: '20260916', planet: PLANET, detail: DETAIL }

/** Every string in the style that could make the browser issue a request. */
function urlish(style: unknown): string[] {
  const found: string[] = []
  JSON.stringify(style, (key, value) => {
    if (typeof value === 'string' && (value.includes('://') || value.startsWith('/'))) found.push(value)
    return value as unknown
  })
  return found
}

describe('buildStyle', () => {
  it('asks for nothing outside this origin', () => {
    // A single third-party host in the style tells that host every map the household opens —
    // the reason the basemap is self-hosted at all.
    for (const flavor of ['dark', 'light'] as const) {
      const urls = urlish(buildStyle(BOTH, flavor))

      expect(urls.length).toBeGreaterThan(0)
      expect(urls.filter((u) => /^https?:\/\//i.test(u))).toEqual([])
      for (const url of urls) {
        expect(url).toMatch(/^(pmtiles:\/\/)?\/api\/map\//)
      }
    }
  })

  it('serves glyphs and sprites from the asset route, per flavor', () => {
    expect(buildStyle(BOTH, 'dark').glyphs).toBe('/api/map/assets/fonts/{fontstack}/{range}.pbf')
    expect(buildStyle(BOTH, 'dark').sprite).toBe('/api/map/assets/sprites/v4/dark')
    expect(buildStyle(BOTH, 'light').sprite).toBe('/api/map/assets/sprites/v4/light')
  })

  it('reads both archives through the pmtiles protocol at their versioned names', () => {
    const style = buildStyle(BOTH, 'dark')

    expect(style.sources.planet).toMatchObject({
      type: 'vector',
      url: 'pmtiles:///api/map/tiles/planet-20260916.pmtiles',
    })
    expect(style.sources.detail).toMatchObject({
      type: 'vector',
      url: 'pmtiles:///api/map/tiles/detail-20260916-a7e95f1d.pmtiles',
    })
  })

  it('hands the world to the planet archive and the pin regions to the detail archive', () => {
    const style = buildStyle(BOTH, 'dark')
    const planet = style.layers.filter((l) => 'source' in l && l.source === 'planet')
    const detail = style.layers.filter((l) => 'source' in l && l.source === 'detail')

    expect(planet.length).toBeGreaterThan(0)
    expect(detail.length).toBeGreaterThan(0)
    for (const layer of planet) {
      expect(layer.maxzoom).toBe(8)
      expect(layer.minzoom ?? 0).toBeLessThan(8)
    }
    for (const layer of detail) {
      expect(layer.minzoom).toBeGreaterThanOrEqual(8)
      expect(layer.maxzoom ?? 24).toBeGreaterThan(layer.minzoom ?? 0)
    }
  })

  it('overzooms the planet archive when no detail archive is published', () => {
    // An overzoomed basemap beats a blank one: without the detail archive, capping the planet
    // layers at z8 would leave every zoom past it empty.
    const style = buildStyle({ build: '20260916', planet: PLANET, detail: null }, 'dark')

    expect(style.sources.detail).toBeUndefined()
    expect(style.layers.every((l) => !('source' in l) || l.source === 'planet')).toBe(true)
    expect(style.layers.filter((l) => 'source' in l && l.maxzoom === 8)).toEqual([])
  })

  it('still renders a map background when no archive exists at all', () => {
    // The route must not depend on the basemap: markers and the grid are the feature.
    const style = buildStyle({ build: null, planet: null, detail: null }, 'light')

    expect(style.sources).toEqual({})
    expect(style.layers.map((l) => l.type)).toEqual(['background'])
  })

  it('names the data it draws without linking anywhere', () => {
    expect(ATTRIBUTION).toBe('© OpenStreetMap contributors © Protomaps')
    expect(JSON.stringify(buildStyle(BOTH, 'dark'))).not.toContain('attribution')
  })
})
