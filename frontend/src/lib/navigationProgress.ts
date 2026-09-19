/**
 * Whether a pending navigation has waited long enough to be worth a bar.
 *
 * Every route but Home is a chunk away, and on a phone over the tunnel the map chunk is a full
 * second of nothing at all: the app looked like it had ignored the tap. A bar on EVERY navigation
 * would be worse — the already-cached ones resolve in a few milliseconds and it would only flash.
 */
export const NAV_PROGRESS_DELAY_MS = 120

/** Whatever the caller uses to tell one navigation from another; the composable passes `to`. */
export type NavigationId = object

export interface NavigationProgressDeps {
  setTimer: (fn: () => void, delayMs: number) => unknown
  clearTimer: (handle: unknown) => void
  onChange?: (visible: boolean) => void
  delayMs?: number
}

export interface NavigationProgress {
  started: (id: NavigationId) => void
  ended: (id: NavigationId) => void
  visible: () => boolean
  stop: () => void
}

/**
 * The bar's whole decision, away from the router and the DOM: WHICH navigation it is speaking for,
 * and whether that one has been out long enough to say so.
 */
export function createNavigationProgress(deps: NavigationProgressDeps): NavigationProgress {
  const delayMs = deps.delayMs ?? NAV_PROGRESS_DELAY_MS
  let current: NavigationId | null = null
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
    current = null
    disarm()
    show(false)
  }

  return {
    started(id) {
      current = id
      // One timer covers the whole wait: it is armed by the first navigation still out, and a
      // second tap mid-wait must not push the bar further away.
      if (visible || timer !== null) return
      timer = deps.setTimer(() => {
        timer = null
        if (current !== null) show(true)
      }, delayMs)
    },
    ended(id) {
      // Only the navigation the bar is speaking for may take it down. The router reports a
      // superseded one only once its chunk finally arrives — taps later — and a redirect never
      // reports the leg it replaced at all; counting either would move the wrong bar.
      if (id !== current) return
      idle()
    },
    visible: () => visible,
    stop: idle,
  }
}
