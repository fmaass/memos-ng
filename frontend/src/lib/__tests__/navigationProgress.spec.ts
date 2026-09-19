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

describe('createNavigationProgress', () => {
  it('shows nothing before a navigation has started', () => {
    const h = harness()
    expect(h.progress.visible()).toBe(false)
    expect(h.scheduled()).toEqual([])
  })

  it('never shows a bar for a navigation that arrives at once', () => {
    // Every already-loaded route is this case. A bar that flashes on Home is worse than no bar.
    const h = harness()
    h.progress.started()
    h.progress.ended('completed')
    h.advance(10_000)

    expect(h.progress.visible()).toBe(false)
    expect(h.seen).toEqual([])
    expect(h.scheduled()).toEqual([])
  })

  it('shows the bar once a navigation outlasts the threshold, and clears it on arrival', () => {
    const h = harness()
    h.progress.started()
    h.advance(NAV_PROGRESS_DELAY_MS - 1)
    expect(h.progress.visible()).toBe(false)

    h.advance(1)
    expect(h.progress.visible()).toBe(true)

    h.progress.ended('completed')
    expect(h.progress.visible()).toBe(false)
    expect(h.seen).toEqual([true, false])
  })

  it('clears the bar when the navigation never gets there', () => {
    // A chunk that fails to load reaches `onError`, not `afterEach`: a bar left spinning would
    // claim the app is still on its way to a view it has given up on.
    const h = harness()
    h.progress.started()
    h.advance(NAV_PROGRESS_DELAY_MS)
    expect(h.progress.visible()).toBe(true)

    h.progress.ended('unfinished')
    expect(h.progress.visible()).toBe(false)
    expect(h.scheduled()).toEqual([])
  })

  it('keeps the newer navigation bar up when the one it superseded reports back', () => {
    // Tapping Places and then Archived cancels the first navigation, and its cancellation arrives
    // while the second is still loading. Hiding on it would leave the second wait unannounced.
    const h = harness()
    h.progress.started()
    h.advance(NAV_PROGRESS_DELAY_MS)
    expect(h.progress.visible()).toBe(true)

    h.progress.started()
    h.progress.ended('unfinished')
    expect(h.progress.visible()).toBe(true)
    expect(h.progress.pending()).toBe(1)

    h.progress.ended('completed')
    expect(h.progress.visible()).toBe(false)
  })

  it('still shows the bar when the first of two navigations was the instant one', () => {
    const h = harness()
    h.progress.started()
    h.progress.started()
    h.progress.ended('unfinished')
    h.advance(NAV_PROGRESS_DELAY_MS)

    expect(h.progress.visible()).toBe(true)
  })

  it('a completed navigation leaves nothing waiting behind it', () => {
    // A redirect reports only the leg that finished, so counting alone can strand the bar up.
    const h = harness()
    h.progress.started()
    h.progress.started()
    h.progress.ended('completed')
    h.advance(10_000)

    expect(h.progress.pending()).toBe(0)
    expect(h.progress.visible()).toBe(false)
  })

  it('does not count below zero when an end arrives with nothing pending', () => {
    const h = harness()
    h.progress.ended('unfinished')
    h.progress.started()
    h.advance(NAV_PROGRESS_DELAY_MS)

    expect(h.progress.pending()).toBe(1)
    expect(h.progress.visible()).toBe(true)
  })

  it('stops cleanly, timer and all', () => {
    const h = harness()
    h.progress.started()
    h.progress.stop()

    expect(h.scheduled()).toEqual([])
    h.advance(10_000)
    expect(h.progress.visible()).toBe(false)
  })
})
