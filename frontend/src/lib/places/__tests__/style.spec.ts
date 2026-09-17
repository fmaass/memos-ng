import { describe, expect, it } from 'vitest'

import { ATTRIBUTION, buildStyle, GLYPHS_URL, type MapManifest } from '@/lib/places/style'

const PLANET = { name: 'planet-20260916.pmtiles', url: '/api/map/tiles/planet-20260916.pmtiles', size: 1 }
const DETAIL = {
  name: 'detail-20260916-a7e95f1d.pmtiles',
  url: '/api/map/tiles/detail-20260916-a7e95f1d.pmtiles',
  size: 2,
}
// No assets revision recorded: the shared fixture is the plain case, and the cases that
// exercise the versioned asset URLs state their own commit.
const BOTH: MapManifest = { build: '20260916', assets_commit: null, planet: PLANET, detail: DETAIL }
const ASSETS_COMMIT = '028c18f713baecad011301ff7a69acc39bcc2ae7'
const OTHER_COMMIT = 'f19c4a0b6d2e5a78c3b1049fd7e6a2158bc0d34e'

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
    const style = buildStyle(
      { build: '20260916', assets_commit: null, planet: PLANET, detail: null },
      'dark',
    )

    expect(style.sources.detail).toBeUndefined()
    expect(style.layers.every((l) => !('source' in l) || l.source === 'planet')).toBe(true)
    expect(style.layers.filter((l) => 'source' in l && l.maxzoom === 8)).toEqual([])
  })

  it('still renders a map background when no archive exists at all', () => {
    // The route must not depend on the basemap: markers and the grid are the feature.
    const style = buildStyle({ build: null, assets_commit: null, planet: null, detail: null }, 'light')

    expect(style.sources).toEqual({})
    expect(style.layers.map((l) => l.type)).toEqual(['background'])
  })

  it('names the data it draws without linking anywhere', () => {
    expect(ATTRIBUTION).toBe('© OpenStreetMap contributors © Protomaps')
    expect(JSON.stringify(buildStyle(BOTH, 'dark'))).not.toContain('attribution')
  })

  it('versions the glyph and sprite URLs with the assets revision being served', () => {
    // fetch-assets.sh overwrites fonts/ and sprites/ in place, and the asset route answers with a
    // week of `immutable`. Without the revision in the URL a browser keeps last week's glyphs for
    // a week after the pin moved; with it, the bump IS a new URL and the old copy is simply never
    // asked for again.
    const style = buildStyle({ ...BOTH, assets_commit: ASSETS_COMMIT }, 'dark')

    expect(style.glyphs).toBe('/api/map/assets/fonts/{fontstack}/{range}.pbf?v=028c18f7')
    expect(style.sprite).toBe('/api/map/assets/sprites/v4/dark?v=028c18f7')
  })

  it('moves every asset URL when the assets revision changes', () => {
    const before = buildStyle({ ...BOTH, assets_commit: ASSETS_COMMIT }, 'light')
    const after = buildStyle({ ...BOTH, assets_commit: OTHER_COMMIT }, 'light')

    expect(after.glyphs).not.toBe(before.glyphs)
    expect(after.sprite).not.toBe(before.sprite)
    expect(after.glyphs).toContain(`?v=${OTHER_COMMIT.slice(0, 8)}`)
    expect(after.sprite).toContain(`?v=${OTHER_COMMIT.slice(0, 8)}`)
    // The archives are untouched by it: their name already carries their version.
    expect(after.sources).toEqual(before.sources)
  })

  it('omits the version query when no assets revision was recorded', () => {
    const style = buildStyle(BOTH, 'dark')

    expect(style.glyphs).toBe(GLYPHS_URL)
    expect(style.sprite).toBe('/api/map/assets/sprites/v4/dark')
  })
})
