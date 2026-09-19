/**
 * The map chunk is the one bundle a tap on Places has to wait for, and it is the only code the
 * service worker does NOT precache (it is a megabyte that a cold Home must not pay for). Warming
 * it once the app is up and the browser is idle puts it in the HTTP cache, so the tap that comes
 * later finds it there.
 */
export const MAP_WARMUP_DELAY_MS = 5_000

/** What the browser has to say about spending bytes nobody asked for, read at firing time. */
export interface WarmupEnvironment {
  online: boolean
  saveData: boolean
  routeName: string | undefined
}

export type WarmupOutcome = 'skipped' | 'warmed' | 'failed'

export function shouldWarmMap(env: WarmupEnvironment): boolean {
  if (!env.online) return false
  // Data saver is the viewer telling the browser to fetch nothing speculatively. This is exactly
  // that kind of fetch.
  if (env.saveData) return false
  // Places is already loading the chunk for real; a second request would only compete with it.
  return env.routeName !== 'places'
}

export interface WarmupDeps {
  readEnvironment: () => WarmupEnvironment
  warm: () => Promise<unknown>
}

export async function runMapWarmup(deps: WarmupDeps): Promise<WarmupOutcome> {
  if (!shouldWarmMap(deps.readEnvironment())) return 'skipped'
  try {
    await deps.warm()
    return 'warmed'
  } catch {
    // An optimisation that failed is not news: the pane's own loading and retry states cover the
    // case where the chunk is really unreachable.
    return 'failed'
  }
}

export interface IdleHost {
  setTimeout: (fn: () => void, delayMs: number) => unknown
  requestIdleCallback?: (fn: () => void, options?: { timeout: number }) => unknown
}

/**
 * Let the app finish its own cold open first, then wait for a gap in the browser's work. Safari
 * has no `requestIdleCallback` at all, so there the delay IS the whole schedule.
 */
export function idleSchedule(host: IdleHost, fn: () => void, delayMs: number): void {
  host.setTimeout(() => {
    const requestIdle = host.requestIdleCallback
    if (requestIdle) requestIdle.call(host, fn, { timeout: 2_000 })
    else fn()
  }, delayMs)
}

export function scheduleMapWarmup(deps: WarmupDeps & { schedule: (fn: () => void) => void }): void {
  deps.schedule(() => {
    void runMapWarmup(deps)
  })
}
