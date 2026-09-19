import { describe, expect, it } from 'vitest'

import { createNavigationProgress, NAV_PROGRESS_DELAY_MS } from '@/lib/navigationProgress'

// A fake timer queue, in the shape reachabilityMonitor's spec uses: the threshold is measured,
// never slept through, so "never flashes" is a fact and not a timing hope.
function harness() {
  let now = 0
  let nextId = 1
  const pending = new Map<number, { at: number; fn: () => void }>()
  const seen: boolean[] = []

  const progress = createNavigationProgress({
    setTimer: (fn, delayMs) => {
      const id = nextId++
      pending.set(id, { at: now + delayMs, fn })
      return id
    },
    clearTimer: (handle) => {
      pending.delete(handle as number)
    },
    onChange: (visible) => seen.push(visible),
  })

  function advance(ms: number) {
    now += ms
    for (const [id, timer] of [...pending]) {
      if (timer.at > now) continue
      pending.delete(id)
      timer.fn()
    }
  }

  return { progress, advance, seen, scheduled: () => [...pending.values()].map((t) => t.at - now) }
}

/** Stands in for the `to` the router hands out — the spec only ever compares identities. */
const navigation = (name: string) => ({ name })

describe('createNavigationProgress', () => {
  it('shows nothing before a navigation has started', () => {
    const h = harness()
    expect(h.progress.visible()).toBe(false)
    expect(h.scheduled()).toEqual([])
  })

  it('never shows a bar for a navigation that arrives at once', () => {
    // Every already-loaded route is this case. A bar that flashes on Home is worse than no bar.
    const h = harness()
    const home = navigation('home')
    h.progress.started(home)
    h.progress.ended(home)
    h.advance(10_000)

    expect(h.progress.visible()).toBe(false)
    expect(h.seen).toEqual([])
    expect(h.scheduled()).toEqual([])
  })

  it('shows the bar once a navigation outlasts the threshold, and clears it on arrival', () => {
    const h = harness()
    const places = navigation('places')
    h.progress.started(places)
    h.advance(NAV_PROGRESS_DELAY_MS - 1)
    expect(h.progress.visible()).toBe(false)

    h.advance(1)
    expect(h.progress.visible()).toBe(true)

    h.progress.ended(places)
    expect(h.progress.visible()).toBe(false)
    expect(h.seen).toEqual([true, false])
  })

  it('clears the bar when the navigation never gets there', () => {
    // A chunk that fails to load reaches `onError`, not `afterEach`: a bar left spinning would
    // claim the app is still on its way to a view it has given up on.
    const h = harness()
    const broken = navigation('broken')
    h.progress.started(broken)
    h.advance(NAV_PROGRESS_DELAY_MS)
    expect(h.progress.visible()).toBe(true)

    h.progress.ended(broken)
    expect(h.progress.visible()).toBe(false)
    expect(h.scheduled()).toEqual([])
  })

  it('keeps the newer navigation bar up when the one it superseded reports back', () => {
    // Tapping Places and then Archived cancels the first navigation, and its cancellation arrives
    // while the second is still loading. Hiding on it would leave the second wait unannounced.
    const h = harness()
    const places = navigation('places')
    const archived = navigation('archived')
    h.progress.started(places)
    h.advance(NAV_PROGRESS_DELAY_MS)
    expect(h.progress.visible()).toBe(true)

    h.progress.started(archived)
    h.progress.ended(places)
    expect(h.progress.visible()).toBe(true)

    h.progress.ended(archived)
    expect(h.progress.visible()).toBe(false)
  })

  it('takes the bar down when the navigation it is showing ends, older ones or not', () => {
    // Resources stalls on its chunk, Settings is tapped and fails outright. The bar speaks for
    // Settings; counting instead would hold it up for as long as the abandoned Resources stalls.
    const h = harness()
    const resources = navigation('resources')
    const settings = navigation('settings')
    h.progress.started(resources)
    h.progress.started(settings)
    h.advance(NAV_PROGRESS_DELAY_MS)
    expect(h.progress.visible()).toBe(true)

    h.progress.ended(settings)
    expect(h.progress.visible()).toBe(false)
    expect(h.scheduled()).toEqual([])
  })

  it('ignores a stale report from a navigation two taps ago', () => {
    // Resources (slow chunk), Explore (cached, arrives), Settings (slow chunk) — and only then
    // the Resources chunk lands and reports itself cancelled. The bar belongs to Settings.
    const h = harness()
    const resources = navigation('resources')
    const explore = navigation('explore')
    const settings = navigation('settings')
    h.progress.started(resources)
    h.progress.started(explore)
    h.progress.ended(explore)
    h.progress.started(settings)
    h.advance(NAV_PROGRESS_DELAY_MS)
    expect(h.progress.visible()).toBe(true)

    h.progress.ended(resources)
    expect(h.progress.visible()).toBe(true)

    h.progress.ended(settings)
    expect(h.progress.visible()).toBe(false)
  })

  it('still shows the bar when the first of two navigations was the instant one', () => {
    const h = harness()
    const first = navigation('first')
    const second = navigation('second')
    h.progress.started(first)
    h.progress.started(second)
    h.progress.ended(first)
    h.advance(NAV_PROGRESS_DELAY_MS)

    expect(h.progress.visible()).toBe(true)
  })

  it('leaves nothing waiting behind the leg of a redirect that finished', () => {
    // A redirect reports only the leg that arrived; the leg it replaced never reports at all.
    const h = harness()
    const asked = navigation('asked')
    const redirected = navigation('redirected')
    h.progress.started(asked)
    h.progress.started(redirected)
    h.progress.ended(redirected)
    h.advance(10_000)

    expect(h.progress.visible()).toBe(false)
    expect(h.scheduled()).toEqual([])
  })

  it('ignores an end that belongs to no navigation it is showing', () => {
    const h = harness()
    h.progress.ended(navigation('stale'))
    h.progress.started(navigation('places'))
    h.advance(NAV_PROGRESS_DELAY_MS)

    expect(h.progress.visible()).toBe(true)
  })

  it('stops cleanly, timer and all', () => {
    const h = harness()
    h.progress.started(navigation('places'))
    h.progress.stop()

    expect(h.scheduled()).toEqual([])
    h.advance(10_000)
    expect(h.progress.visible()).toBe(false)
  })
})
