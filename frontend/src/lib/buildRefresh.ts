/**
 * What to do when the build the open page was served has been replaced under it.
 *
 * Two symptoms, one cause. A lazy route chunk 404s because the hashed file it asks for is no
 * longer on the server (a deploy replaced it), and the router's default handling of that is
 * silence: the navigation simply never happens and the tap looks dead. And a page left open
 * across a deploy keeps running the old build until something makes it fetch the shell again.
 *
 * Every decision here is a pure function — no `window`, no `document`, no `sessionStorage`: the
 * storage, the clock and the navigation target are passed in, so the loop guard (the part that
 * MUST be right, because getting it wrong means a reload cycle) is unit-testable directly.
 */

/** The slice of `Storage` the marker needs — `sessionStorage` satisfies it structurally. */
export interface ReloadMarkerStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

/**
 * How long one reload attempt for a target keeps counting. Long enough that a reload plus the
 * user's next tap on the same dead route still sees the marker (so the guard holds), short
 * enough that a genuinely transient network failure hours later is allowed one fresh reload.
 */
export const CHUNK_RELOAD_MARKER_TTL_MS = 10 * 60_000

/** How often an open page asks the service worker whether a newer build exists. */
export const BUILD_UPDATE_POLL_MS = 60 * 60_000

/** The toast text for the case where reloading did not help — the user has to act. */
export const NEW_BUILD_MESSAGE = 'A new version is available — reload the page'

const MARKER_PREFIX = 'memos-ng:chunk-reload:'

/** One marker per target path: a dead /places must not stop /search from reloading once. */
export function chunkReloadMarkerKey(target: string): string {
  return `${MARKER_PREFIX}${target}`
}

// Each browser words a failed dynamic import differently, and none of them types it: the message
// is all there is to go on. Chrome/Edge, Safari, Firefox, then Vite's own CSS-preload failure
// (the map chunk's stylesheet is fetched through that helper).
const CHUNK_ERROR_FRAGMENTS = [
  'failed to fetch dynamically imported module',
  'importing a module script failed',
  'error loading dynamically imported module',
  'unable to preload css',
]

export function isChunkLoadError(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  const message = error.message.toLowerCase()
  return CHUNK_ERROR_FRAGMENTS.some((fragment) => message.includes(fragment))
}

export type ChunkFailureAction = 'reload' | 'message' | 'ignore'

export interface ChunkFailureInput {
  error: unknown
  /** The path the navigation was heading to — what a reload would land on. */
  target: string
  storage: ReloadMarkerStorage
  now: number
}

/**
 * `reload` at most once per target: the second failure means the reload did not fix it (a
 * genuinely missing asset, an offline device), and reloading again would spin. That case gets a
 * message the user can act on instead.
 */
export function decideOnChunkFailure(input: ChunkFailureInput): { action: ChunkFailureAction } {
  if (!isChunkLoadError(input.error)) return { action: 'ignore' }

  const key = chunkReloadMarkerKey(input.target)
  // Blocked cookies/storage and Safari's private mode make every `sessionStorage` touch throw —
  // and without storage there is no loop guard, so reloading would be a bet on the reload
  // working. The message is the safe half of the contract: it never loops.
  let raw: string | null
  try {
    raw = input.storage.getItem(key)
  } catch {
    return { action: 'message' }
  }

  // `Number(null)` is 0, which would read as a marker stamped at the epoch — an absent marker has
  // to be turned into NaN by hand or the very first failure decides it has already reloaded.
  const stamped = raw ? Number(raw) : Number.NaN
  const age = input.now - stamped
  // A marker that is unreadable, or stamped in the future by a clock change, counts as absent.
  if (Number.isFinite(stamped) && age >= 0 && age < CHUNK_RELOAD_MARKER_TTL_MS) {
    return { action: 'message' }
  }

  try {
    input.storage.setItem(key, String(input.now))
  } catch {
    // The marker could not be recorded, so a reload would come back to exactly this point.
    return { action: 'message' }
  }
  return { action: 'reload' }
}

/** Where the document is right now — only the parts that decide whether `assign` navigates. */
export interface CurrentLocation {
  pathname: string
  search: string
}

export type FullLoadPlan = { kind: 'assign'; url: string } | { kind: 'reload' }

/**
 * `location.assign(url)` for a URL that differs from the current one only by its fragment is a
 * same-document navigation: the fragment scrolls, nothing is fetched, and the stale build stays.
 * Same for assigning the identical URL. Those cases need `location.reload()` instead.
 */
export function decideFullLoad({
  target,
  current,
}: {
  target: string
  current: CurrentLocation
}): FullLoadPlan {
  const hashAt = target.indexOf('#')
  const beforeHash = hashAt === -1 ? target : target.slice(0, hashAt)
  const queryAt = beforeHash.indexOf('?')
  const path = queryAt === -1 ? beforeHash : beforeHash.slice(0, queryAt)
  const search = queryAt === -1 ? '' : beforeHash.slice(queryAt)

  if (path === current.pathname && search === current.search) return { kind: 'reload' }
  return { kind: 'assign', url: target }
}

/** Whether this page owes the user a full load onto the build that took over. */
export interface BuildRefreshState {
  fullLoadDue: boolean
}

export function initialBuildRefreshState(): BuildRefreshState {
  return { fullLoadDue: false }
}

/**
 * `controllerchange` fires both when a service worker installs for the first time and when a new
 * build replaces the running one. Only the second is an update: on a first install the page is
 * already running the build the worker just cached, so a full load would buy nothing.
 */
export function noteControllerChange(
  state: BuildRefreshState,
  { hadController }: { hadController: boolean },
): BuildRefreshState {
  return { fullLoadDue: state.fullLoadDue || hadController }
}

/**
 * Deliberately NOT an immediate reload: the new build takes over while the page is in use, and a
 * reload then throws away whatever is being typed. The next navigation is already a screen
 * change, so it is the cheapest moment to pay for a full load.
 */
export function shouldFullLoadOnNavigation(state: BuildRefreshState): boolean {
  return state.fullLoadDue
}

export function consumeFullLoad(state: BuildRefreshState): BuildRefreshState {
  return { ...state, fullLoadDue: false }
}

/** A background tab polling for updates spends the user's bytes for nothing. */
export function shouldCheckForUpdate({ visibility }: { visibility: string }): boolean {
  return visibility === 'visible'
}

export interface UpdateCheckDeps {
  registration: { update(): Promise<unknown> }
  visibility: () => string
  schedule: (check: () => void, everyMs: number) => unknown
}

/**
 * Asks the service worker whether a newer build exists, on an interval, while the page is
 * visible. The check is a network request like any other: offline, or a 502 from the proxy, it
 * rejects — and an unhandled rejection there would surface as an error the user cannot act on.
 */
export function scheduleUpdateChecks(deps: UpdateCheckDeps): void {
  deps.schedule(() => {
    if (!shouldCheckForUpdate({ visibility: deps.visibility() })) return
    void deps.registration.update().catch(() => undefined)
  }, BUILD_UPDATE_POLL_MS)
}
