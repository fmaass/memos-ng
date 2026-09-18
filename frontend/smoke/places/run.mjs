#!/usr/bin/env node
// The Places release smoke: a real headless browser against the PRODUCTION build.
//
//   npm run build && npm run smoke:places
//
// It exists because happy-dom cannot run MapLibre. In 1.7.0, 913 green unit tests and two audit
// rounds shipped a map with no icons (the sprite address was root-relative, and MapLibre refuses
// a sprite URL it cannot parse as absolute) and a first view that put pins underneath the
// timeline bar. Both are console-or-geometry facts a browser sees in a second and a DOM double
// cannot see at all. So: any console error fails, and any first-view pin outside the visible,
// unobscured map pane fails — on a desktop viewport and on a phone one.
//
// Browser: a Chromium the machine already has. `PLACES_SMOKE_CHROME` wins; otherwise the newest
// Playwright browser build under `PLAYWRIGHT_BROWSERS_PATH` or `~/.cache/ms-playwright`, then
// `/usr/bin/chromium`. `playwright-core` is a devDependency that downloads nothing on install.

import { existsSync, mkdirSync, readdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { chromium } from 'playwright-core'

import { createSmokeServer, SMOKE_MANIFEST } from './server.mjs'

const VIEWPORTS = [
  { label: 'desktop', width: 1440, height: 900 },
  { label: 'phone', width: 390, height: 844 },
]
const PORT = Number(process.env.PLACES_SMOKE_PORT ?? 8741)
const SETTLE_MS = Number(process.env.PLACES_SMOKE_SETTLE_MS ?? 6000)
const SHOTS = process.env.PLACES_SMOKE_SHOTS ?? ''

function findChrome() {
  if (process.env.PLACES_SMOKE_CHROME) return process.env.PLACES_SMOKE_CHROME
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || join(homedir(), '.cache/ms-playwright')
  if (existsSync(root)) {
    const builds = readdirSync(root)
      .filter((name) => /^chromium-\d+$/.test(name))
      .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]))
    for (const build of builds) {
      for (const relative of ['chrome-linux64/chrome', 'chrome-linux/chrome', 'chrome-mac/Chromium.app/Contents/MacOS/Chromium']) {
        const path = join(root, build, relative)
        if (existsSync(path)) return path
      }
    }
  }
  for (const path of ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome']) {
    if (existsSync(path)) return path
  }
  throw new Error(
    'No Chromium found. Set PLACES_SMOKE_CHROME to a Chromium/Chrome binary, or install one ' +
      '(`npx playwright install chromium`, or the system chromium package).',
  )
}

const failures = []
function check(ok, message) {
  if (!ok) failures.push(message)
  return ok
}

async function runViewport(browser, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()
  const consoleErrors = []
  const failedResponses = []
  const requests = []
  page.on('request', (request) => requests.push(request.url()))
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('pageerror', (error) => consoleErrors.push(`pageerror: ${String(error)}`))
  page.on('response', (response) => {
    if (response.status() >= 400) failedResponses.push(`${response.status()} ${response.url()}`)
  })

  await page.goto(`http://127.0.0.1:${PORT}/places`, { waitUntil: 'load' })
  await page.waitForTimeout(SETTLE_MS)

  const seen = await page.evaluate(() => {
    const rect = (el) => {
      if (!el) return null
      const b = el.getBoundingClientRect()
      return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) }
    }
    const pane = document.querySelector('[data-test="places-map"]')
    return {
      pane: rect(pane),
      bar: rect(document.querySelector('[data-test="timeline-bar"]')),
      pins: [...document.querySelectorAll('.maplibregl-marker')].map(rect),
    }
  })

  const origin = `http://127.0.0.1:${PORT}`

  const label = `[${viewport.label} ${viewport.width}x${viewport.height}]`
  check(!!seen.pane, `${label} no map pane rendered`)
  check(seen.pins.length > 0, `${label} no pins rendered — the smoke proves nothing without them`)

  // Every pin of the FIRST view must be inside the pane and clear of the bar drawn over it.
  if (seen.pane) {
    for (const pin of seen.pins) {
      const inside =
        pin.x >= seen.pane.x &&
        pin.y >= seen.pane.y &&
        pin.x + pin.w <= seen.pane.x + seen.pane.w &&
        pin.y + pin.h <= seen.pane.y + seen.pane.h
      check(inside, `${label} pin at ${JSON.stringify(pin)} is outside the map pane ${JSON.stringify(seen.pane)}`)
      if (seen.bar) {
        const obscured =
          pin.x + pin.w > seen.bar.x &&
          pin.x < seen.bar.x + seen.bar.w &&
          pin.y + pin.h > seen.bar.y &&
          pin.y < seen.bar.y + seen.bar.h
        check(!obscured, `${label} pin at ${JSON.stringify(pin)} is behind the timeline bar ${JSON.stringify(seen.bar)}`)
      }
    }
  }

  // The sprite is the defect class this smoke exists for: MapLibre parses that address with
  // `new URL(url)` and NO base, so a root-relative one throws before a single request is made.
  // Seeing the sheet fetched from THIS origin is therefore the positive proof, and the console
  // check above is what catches the refusal itself.
  const sprites = requests.filter((url) => url.includes('/api/map/assets/sprites/'))
  check(
    sprites.length > 0,
    `${label} MapLibre never requested the sprite sheet — an address it refuses makes no request`,
  )
  for (const url of sprites) {
    check(new URL(url).origin === origin, `${label} sprite request left this origin: ${url}`)
  }
  check(
    sprites.some((url) => url.endsWith('.json') || url.includes('.json?')),
    `${label} sprite index was never fetched: ${sprites.join(' | ')}`,
  )
  // Not a MapLibre request: this style has no symbol layer (no basemap archive is committed),
  // so MapLibre asks for no glyph range at all here. What this proves is narrower and worth
  // stating exactly — the font route answers 200 on this origin, at the versioned address the
  // style builds, fetched from the page itself. The TEMPLATE's shape (absolute, same-origin,
  // `{fontstack}` and `{range}` intact) is pinned by the unit tests in `style.spec.ts`; reading
  // it off the live style would need a test-only hook in production code, which is not worth it.
  const glyphs = await page.evaluate(async ([base, revision]) => {
    const query = revision ? `?v=${revision.slice(0, 8)}` : ''
    const url = `${base}/api/map/assets/fonts/Noto%20Sans%20Regular/0-255.pbf${query}`
    try {
      return { url, status: (await fetch(url)).status }
    } catch {
      return { url, status: 0 }
    }
  }, [origin, SMOKE_MANIFEST.assets_commit])
  check(glyphs.status === 200, `${label} glyph range did not answer from this origin: ${glyphs.url} (${glyphs.status})`)

  if (SHOTS) {
    mkdirSync(SHOTS, { recursive: true })
    await page.screenshot({ path: join(SHOTS, `places-smoke-${viewport.label}.png`) })
  }
  // LAST, and after everything above that can still produce one: the screenshot, the glyph
  // fetch and the page's own late work all happen after the settle window, and an error there
  // used to be printed in the summary and never fail the run.
  await context.close()
  check(
    consoleErrors.length === 0,
    `${label} console errors (${consoleErrors.length}): ${consoleErrors.join(' | ')}`,
  )
  check(
    failedResponses.length === 0,
    `${label} requests that failed: ${failedResponses.join(' | ')}`,
  )
  // Self-hosting the basemap is only worth anything if the page asks NOTHING of anyone else:
  // one foreign request would tell that host every place the household has been. Non-network
  // schemes carry no such request.
  const foreign = [
    ...new Set(
      requests.filter((url) => {
        if (/^(data|blob|about):/i.test(url)) return false
        try {
          return new URL(url).origin !== origin
        } catch {
          return true
        }
      }),
    ),
  ]
  check(foreign.length === 0, `${label} requests left this origin: ${foreign.join(' | ')}`)

  // Printed from the very arrays the checks above read, so the line cannot disagree with the
  // verdict.
  process.stdout.write(
    `${label} pins ${seen.pins.length}, console errors ${consoleErrors.length}, ` +
      `failed requests ${failedResponses.length}, foreign requests ${foreign.length}, ` +
      `bar ${JSON.stringify(seen.bar)}\n`,
  )
}

const { server } = createSmokeServer()
await new Promise((done) => server.listen(PORT, '127.0.0.1', done))
const browser = await chromium.launch({ executablePath: findChrome(), headless: true })
try {
  for (const viewport of VIEWPORTS) await runViewport(browser, viewport)
} finally {
  await browser.close()
  server.close()
}

if (failures.length) {
  process.stdout.write(`\nplaces smoke FAILED (${failures.length}):\n`)
  for (const failure of failures) process.stdout.write(`  - ${failure}\n`)
  process.exit(1)
}
process.stdout.write('\nplaces smoke OK\n')
