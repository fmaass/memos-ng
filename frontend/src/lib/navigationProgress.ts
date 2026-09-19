/**
 * Whether a pending navigation has waited long enough to be worth a bar.
 *
 * Every route but Home is a chunk away, and on a phone over the tunnel the map chunk is a full
 * second of nothing at all: the app looked like it had ignored the tap. A bar on EVERY navigation
 * would be worse — the already-cached ones resolve in a few milliseconds and it would only flash.
 */
export const NAV_PROGRESS_DELAY_MS = 120

/** How a navigation left: on the view it asked for, or any other way. */
export type NavigationEnd = 'completed' | 'unfinished'

export interface NavigationProgressDeps {
  setTimer: (fn: () => void, delayMs: number) => unknown
  clearTimer: (handle: unknown) => void
  onChange?: (visible: boolean) => void
  delayMs?: number
}

export interface NavigationProgress {
  started: () => void
  ended: (end: NavigationEnd) => void
  visible: () => boolean
  pending: () => number
  stop: () => void
}

/**
 * The bar's whole decision, away from the router and the DOM: how many navigations are out, and
 * whether the oldest of them has been out long enough to say so.
 */
export function createNavigationProgress(deps: NavigationProgressDeps): NavigationProgress {
  const delayMs = deps.delayMs ?? NAV_PROGRESS_DELAY_MS
  let pending = 0
  let visible = false
  let timer: unknown = null

  function show(next: boolean) {
    if (visible === next) return
    visible = next
    deps.onChange?.(next)
  }

  function disarm() {
    if (timer === null) return
    deps.clearTimer(timer)
    timer = null
  }

  function idle() {
    pending = 0
    disarm()
    show(false)
  }

  return {
    started() {
      pending += 1
      // One timer covers the whole queue: it is armed by the first navigation still out, and a
      // second one starting mid-wait must not push the bar further away.
      if (visible || timer !== null) return
      timer = deps.setTimer(() => {
        timer = null
        if (pending > 0) show(true)
      }, delayMs)
    },
    ended(end) {
      // A completed navigation has its view on screen, so nothing may still be waiting behind it:
      // a redirect reports only the leg that finished, and counting alone would strand the bar.
      if (end === 'completed') {
        idle()
        return
      }
      pending = Math.max(0, pending - 1)
      if (pending === 0) idle()
    },
    visible: () => visible,
    pending: () => pending,
    stop: idle,
  }
}
