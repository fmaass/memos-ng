// One origin that serves the PRODUCTION build of the SPA plus the handful of API routes the
// Places view needs, so a real browser can be pointed at it.
//
// Everything the map style asks for — the sprite sheet above all — is fetched over HTTP from
// THIS origin, which is the point: MapLibre's real sprite loader runs, and so does the
// `new URL(url)` validation that silently refused a root-relative address in 1.7.0 and left the
// basemap without a single icon. happy-dom cannot run MapLibre, so no unit test can see that.
//
// Every byte served here is synthetic and committed next to this file: the smoke is a release
// gate and may not depend on a host's tiles share, a database or the network.

import { readFileSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { dirname, extname, join, normalize, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const DIST = process.env.PLACES_SMOKE_DIST ?? resolve(HERE, '../../dist')
const FIXTURES = join(HERE, 'fixtures')

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.webmanifest': 'application/manifest+json',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.eot': 'application/vnd.ms-fontobject',
  '.ico': 'image/x-icon',
  '.pbf': 'application/x-protobuf',
}

/** A 1x1 transparent PNG — every pin thumbnail, so no <img> 404 reaches the console. */
const PIXEL = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)

/**
 * A synthetic library: `count` photos on invented coordinates, spread over `months` monthly
 * buckets. Nothing here comes from anyone's library.
 */
export function synthesizeMarkers(count = 8, months = 4) {
  return Array.from({ length: count }, (_, i) => {
    const uid = `smoke-${String(i).padStart(4, '0')}`
    const when = new Date(Date.UTC(2026, i % months, 5 + (i % 20), 9, 0, 0)).toISOString()
    return {
      uid,
      mime: 'image/jpeg',
      status: 'ready',
      blurhash: null,
      width: 1200,
      height: 800,
      duration: null,
      sha256: null,
      capture_instant: when,
      capture_civil: when.slice(0, 19),
      capture_source: 'exif',
      mine: true,
      hasPrint: false,
      hasLegacyPrint: false,
      renditions: [],
      position: i,
      thumbnailUrl: `/api/smoke/pixel/${uid}-thumb.png`,
      previewUrl: `/api/smoke/pixel/${uid}-preview.png`,
      webUrl: `/api/smoke/pixel/${uid}-web.png`,
      posterUrl: `/api/smoke/pixel/${uid}-poster.png`,
      downloadUrl: `/api/smoke/pixel/${uid}.png`,
      printUrl: `/api/smoke/pixel/${uid}-print.png`,
      memo_uid: `smoke-memo-${String(i % 5).padStart(3, '0')}`,
      // An invented patch of open ocean: no real place, and far enough apart to cluster.
      latitude: -31.5 + (i % 7) * 0.05,
      longitude: -14.2 + (i % 5) * 0.08,
      taken_at: when,
    }
  })
}

/**
 * The manifest the fixture serves. Exported because the style versions its asset addresses with
 * `assets_commit`, and the runner's font-route probe has to ask for the same address the style
 * would build — including that query.
 */
export const SMOKE_MANIFEST = {
  build: '00000000',
  assets_commit: '0123456789abcdef0123456789abcdef01234567',
  planet: null,
  detail: null,
  uncovered: null,
}

function send(res, status, body, headers = {}) {
  res.writeHead(status, { 'Cache-Control': 'no-store', ...headers })
  res.end(body)
}

const json = (res, value) =>
  send(res, 200, JSON.stringify(value), { 'Content-Type': MIME['.json'] })

/** Refuse anything that resolves outside the directory it is supposed to come from. */
function contain(base, relative) {
  const root = resolve(base)
  const candidate = resolve(join(root, normalize('/' + relative)))
  return candidate.startsWith(root + '/') ? candidate : null
}

function sendFile(res, path) {
  try {
    statSync(path)
  } catch {
    return send(res, 404, 'not found')
  }
  send(res, 200, readFileSync(path), {
    'Content-Type': MIME[extname(path)] ?? 'application/octet-stream',
  })
}

export function createSmokeServer({ markers = 8, months = 4 } = {}) {
  const unstubbed = new Set()
  const server = createServer((req, res) => {
    const path = new URL(req.url, 'http://localhost').pathname

    if (path === '/api/map/manifest') {
      // No basemap archive: a PMTiles planet extract is gigabytes and cannot be committed. The
      // style is then the background colour plus the sprite and glyph addresses — which is all
      // this smoke needs, because the sprite is fetched eagerly at style load and that fetch is
      // the defect class (`Invalid sprite URL …, must be absolute`).
      return json(res, SMOKE_MANIFEST)
    }
    if (path === '/api/map/markers') return json(res, { markers: synthesizeMarkers(markers, months) })
    if (path.startsWith('/api/map/assets/sprites/')) {
      const file = path.endsWith('.png') ? 'sprite.png' : 'sprite.json'
      return sendFile(res, join(FIXTURES, file))
    }
    if (path.startsWith('/api/map/assets/fonts/')) {
      // An empty protobuf body: a valid, glyph-less range. Nothing in this smoke's style has a
      // symbol layer, so this answers the probe request the runner makes, not a MapLibre one.
      return send(res, 200, Buffer.alloc(0), { 'Content-Type': MIME['.pbf'] })
    }
    if (path.startsWith('/api/smoke/pixel/')) {
      return send(res, 200, PIXEL, { 'Content-Type': MIME['.png'] })
    }
    if (path === '/api/auth/user') {
      return json(res, {
        subject: 'smoke',
        display_name: 'Smoke',
        email: 'smoke@example.invalid',
        role: 'member',
        auth_kind: 'session',
      })
    }
    if (path === '/api/tags') return json(res, { tags: [] })
    if (path === '/api/settings') return json(res, {})
    if (path === '/api/events') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' })
      return res.write(': smoke\n\n')
    }
    if (path.startsWith('/api/') || path.startsWith('/file/')) {
      unstubbed.add(path)
      return json(res, {})
    }

    const file = contain(DIST, path === '/' ? '/index.html' : path)
    if (file) {
      try {
        statSync(file)
        return sendFile(res, file)
      } catch {
        /* a client-side route: fall through to the shell */
      }
    }
    sendFile(res, join(DIST, 'index.html'))
  })
  return { server, unstubbed }
}
