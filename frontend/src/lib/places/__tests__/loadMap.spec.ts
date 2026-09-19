// @vitest-environment jsdom
// happy-dom really fetches a `<link rel=stylesheet>` the moment it is appended, which in a test
// is always a failure and would record links these tests never meant to break.
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  loadPlacesMap,
  recordStylesheetFailures,
  repairFailedStylesheets,
} from '@/lib/places/loadMap'

// The chunk itself is a megabyte of MapLibre and is not what these tests are about: what matters
// is what has to be true about the document BEFORE it is imported.
vi.mock('@/components/places/PlacesMap.vue', () => ({ default: { name: 'PlacesMap' } }))

const HREF = 'https://memos.test/assets/map-abc123.css'

function failedLink(href = HREF): HTMLLinkElement {
  const link = document.createElement('link')
  link.rel = 'stylesheet'
  link.href = href
  document.head.append(link)
  // Exactly what the browser does when the stylesheet Vite's preload helper appended never
  // arrived: an `error` at the element, which does not bubble.
  link.dispatchEvent(new Event('error'))
  return link
}

const linkFor = (href = HREF) =>
  document.head.querySelector<HTMLLinkElement>(`link[href="${href}"]`)

/** Let the repair get as far as it can: it then waits on the replacement's own events. */
async function settle() {
  for (let round = 0; round < 3; round++) {
    for (let i = 0; i < 10; i++) await Promise.resolve()
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
}

beforeEach(async () => {
  // The list of failed links outlives a test, as it outlives a navigation in the browser, and
  // only a replacement that loads takes one off it. So: repair whatever is left over, let every
  // replacement load, and each test starts on a document that has failed nothing.
  const drain = repairFailedStylesheets()
  for (const link of document.head.querySelectorAll('link')) link.dispatchEvent(new Event('load'))
  await drain.catch(() => {})
  document.head.replaceChildren()
  recordStylesheetFailures(document)
})

describe('repairFailedStylesheets', () => {
  it('replaces a link the document has already failed, so the request goes out again', async () => {
    const broken = failedLink()

    const repair = repairFailedStylesheets()
    await settle()

    const fresh = linkFor()
    // A NEW element is the whole point: Vite's helper has marked this dependency seen and will
    // never append it again, and the browser will not re-fetch the element that failed.
    expect(fresh).not.toBeNull()
    expect(fresh).not.toBe(broken)
    expect(broken.isConnected).toBe(false)

    fresh!.dispatchEvent(new Event('load'))
    await expect(repair).resolves.toBeUndefined()
  })

  it('gives up when the replacement fails too', async () => {
    failedLink()
    const repair = repairFailedStylesheets()
    await settle()

    linkFor()!.dispatchEvent(new Event('error'))

    // The pane's own failure path takes it from here: an unstyled map that looks broken is worse
    // than a pane that says it could not load.
    await expect(repair).rejects.toThrow(/map-abc123\.css/)
  })

  it('leaves a document nothing failed in untouched', async () => {
    const healthy = document.createElement('link')
    healthy.rel = 'stylesheet'
    healthy.href = HREF
    document.head.append(healthy)

    await expect(repairFailedStylesheets()).resolves.toBeUndefined()
    expect(linkFor()).toBe(healthy)
  })

  it('ignores an error from anything that is not a stylesheet', async () => {
    const preload = document.createElement('link')
    preload.rel = 'modulepreload'
    preload.href = 'https://memos.test/assets/map-abc123.js'
    document.head.append(preload)
    preload.dispatchEvent(new Event('error'))

    await expect(repairFailedStylesheets()).resolves.toBeUndefined()
    // A module that failed is the loader's own business — re-appending its preload would fetch a
    // megabyte nobody asked for.
    expect(document.head.querySelector('link[rel="modulepreload"]')).toBe(preload)
  })
})

describe('loadPlacesMap', () => {
  it('holds the import until a stylesheet that failed before is back', async () => {
    failedLink()
    const settled = vi.fn()
    void loadPlacesMap().then(settled, settled)
    await settle()

    // Asserted at the decision point: the map may not mount while its positioning styles are
    // missing — markers and controls would sit in the wrong place with nothing to say so.
    expect(settled).not.toHaveBeenCalled()

    linkFor()!.dispatchEvent(new Event('load'))
    await settle()
    expect(settled).toHaveBeenCalledTimes(1)
  })

  it('holds every caller on the one repair that is in flight', async () => {
    // The warm-up and the pane share this loader, so both can want the repair at once; the viewer
    // leaving Places and coming straight back does it too. A caller that walked past an
    // unfinished repair would import the chunk with the stylesheet still missing, and Vite's
    // helper never appends that one again — an unstyled map, for good.
    failedLink()
    const settled: string[] = []
    const record = (name: string) => (outcome: unknown) => {
      settled.push(outcome instanceof Error ? `${name} failed` : name)
    }
    void repairFailedStylesheets().then(record('first'), record('first'))
    void loadPlacesMap().then(record('loader'), record('loader'))
    void repairFailedStylesheets().then(record('second'), record('second'))
    await settle()

    // One failed link, one replacement: the later callers joined the repair that was running
    // rather than each starting one of their own.
    const replacements = document.head.querySelectorAll(`link[href="${HREF}"]`)
    expect(replacements).toHaveLength(1)
    expect(settled).toEqual([])

    replacements[0].dispatchEvent(new Event('load'))
    await settle()
    expect([...settled].sort()).toEqual(['first', 'loader', 'second'])
  })

  it('keeps the failure recorded when the replacement fails, and tries again after', async () => {
    failedLink()
    const first = repairFailedStylesheets()
    const second = repairFailedStylesheets()
    await settle()

    const replacement = linkFor()!
    replacement.dispatchEvent(new Event('error'))
    await expect(first).rejects.toThrow(/map-abc123\.css/)
    await expect(second).rejects.toThrow(/map-abc123\.css/)

    // The rejection reaches every caller — and the next attempt is a real one: the failure is
    // still on the list, and a NEW element, because the browser would answer the burned one from
    // its memory of that failure.
    const third = loadPlacesMap()
    await settle()
    const next = linkFor()!
    expect(next).not.toBe(replacement)

    next.dispatchEvent(new Event('load'))
    await expect(third).resolves.toMatchObject({ default: { name: 'PlacesMap' } })
  })

  it('imports straight away when no stylesheet has failed', async () => {
    await expect(loadPlacesMap()).resolves.toMatchObject({ default: { name: 'PlacesMap' } })
  })
})
