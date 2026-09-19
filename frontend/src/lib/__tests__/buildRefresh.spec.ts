import { describe, expect, it, vi } from 'vitest'

import {
  BUILD_UPDATE_POLL_MS,
  CHUNK_RELOAD_MARKER_TTL_MS,
  consumeFullLoad,
  decideFullLoad,
  decideOnChunkFailure,
  initialBuildRefreshState,
  isChunkLoadError,
  noteControllerChange,
  scheduleUpdateChecks,
  shouldCheckForUpdate,
  shouldFullLoadOnNavigation,
  type ReloadMarkerStorage,
} from '../buildRefresh'

/** A real (in-memory) Storage-shaped double: the decision has to survive a round trip. */
function fakeStorage(): ReloadMarkerStorage & { entries: Map<string, string> } {
  const entries = new Map<string, string>()
  return {
    entries,
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => {
      entries.set(key, value)
    },
    removeItem: (key) => {
      entries.delete(key)
    },
  }
}

describe('isChunkLoadError', () => {
  it('recognises Chrome/Edge: failed to fetch dynamically imported module', () => {
    expect(
      isChunkLoadError(
        new TypeError(
          'Failed to fetch dynamically imported module: https://memos.discomarder.live/assets/PlacesView-a1b2c3.js',
        ),
      ),
    ).toBe(true)
  })

  it('recognises Safari: importing a module script failed', () => {
    expect(isChunkLoadError(new TypeError('Importing a module script failed.'))).toBe(true)
  })

  it('recognises Firefox: error loading dynamically imported module', () => {
    expect(
      isChunkLoadError(
        new TypeError(
          'error loading dynamically imported module: https://memos.discomarder.live/assets/SearchView-9f8e7d.js',
        ),
      ),
    ).toBe(true)
  })

  it("recognises Vite's CSS preload failure", () => {
    expect(isChunkLoadError(new Error('Unable to preload CSS for /assets/map-4d5e6f.css'))).toBe(
      true,
    )
  })

  it('is false for an ordinary application error', () => {
    expect(isChunkLoadError(new Error('Cannot read properties of undefined (reading uid)'))).toBe(
      false,
    )
  })

  it('is false for a non-Error value, even one that reads like a chunk failure', () => {
    expect(isChunkLoadError('Failed to fetch dynamically imported module: /assets/x.js')).toBe(false)
    expect(isChunkLoadError(null)).toBe(false)
  })
})

describe('decideOnChunkFailure', () => {
  const chunkError = new TypeError('Failed to fetch dynamically imported module: /assets/x.js')

  it('reloads the first time a target fails, and marks that target', () => {
    const storage = fakeStorage()

    expect(
      decideOnChunkFailure({ error: chunkError, target: '/places', storage, now: 1_000 }),
    ).toEqual({ action: 'reload' })

    const keys = [...storage.entries.keys()]
    expect(keys).toHaveLength(1)
    expect(keys[0]).toContain('/places')
    expect(Number(storage.entries.get(keys[0]!))).toBe(1_000)
  })

  it('shows a message instead of reloading a second time for the same target', () => {
    const storage = fakeStorage()
    decideOnChunkFailure({ error: chunkError, target: '/places', storage, now: 1_000 })
    const write = vi.spyOn(storage, 'setItem')

    expect(
      decideOnChunkFailure({ error: chunkError, target: '/places', storage, now: 5_000 }),
    ).toEqual({ action: 'message' })
    expect(write).not.toHaveBeenCalled()
  })

  it('still reloads once for a different target while another marker is fresh', () => {
    const storage = fakeStorage()
    decideOnChunkFailure({ error: chunkError, target: '/places', storage, now: 1_000 })

    expect(
      decideOnChunkFailure({ error: chunkError, target: '/search', storage, now: 2_000 }),
    ).toEqual({ action: 'reload' })
  })

  it('allows one new reload once the marker has expired', () => {
    const storage = fakeStorage()
    decideOnChunkFailure({ error: chunkError, target: '/places', storage, now: 1_000 })
    const later = 1_000 + CHUNK_RELOAD_MARKER_TTL_MS + 1

    expect(
      decideOnChunkFailure({ error: chunkError, target: '/places', storage, now: later }),
    ).toEqual({ action: 'reload' })
    const key = [...storage.entries.keys()][0]!
    expect(Number(storage.entries.get(key))).toBe(later)
  })

  it('keeps the marker lifetime in minutes, not days', () => {
    expect(CHUNK_RELOAD_MARKER_TTL_MS).toBeGreaterThan(60_000)
    expect(CHUNK_RELOAD_MARKER_TTL_MS).toBeLessThanOrEqual(60 * 60_000)
  })

  it('ignores an error that is not a chunk failure and writes nothing', () => {
    const storage = fakeStorage()

    expect(
      decideOnChunkFailure({
        error: new Error('memo 42 not found'),
        target: '/m/42',
        storage,
        now: 1_000,
      }),
    ).toEqual({ action: 'ignore' })
    expect(storage.entries.size).toBe(0)
  })

  it('ignores a corrupted marker by treating it as absent', () => {
    const storage = fakeStorage()
    decideOnChunkFailure({ error: chunkError, target: '/places', storage, now: 1_000 })
    const key = [...storage.entries.keys()][0]!
    storage.entries.set(key, 'not-a-timestamp')

    expect(
      decideOnChunkFailure({ error: chunkError, target: '/places', storage, now: 1_500 }),
    ).toEqual({ action: 'reload' })
  })
})

describe('decideOnChunkFailure with storage that throws', () => {
  const chunkError = new TypeError('Failed to fetch dynamically imported module: /assets/x.js')
  const boom = () => {
    throw new DOMException('The operation is insecure.', 'SecurityError')
  }

  it('shows the message rather than reloading blind when the marker cannot be read', () => {
    const storage: ReloadMarkerStorage = { getItem: boom, setItem: boom, removeItem: boom }

    expect(
      decideOnChunkFailure({ error: chunkError, target: '/places', storage, now: 1_000 }),
    ).toEqual({ action: 'message' })
  })

  it('shows the message when the marker cannot be written', () => {
    const readable = fakeStorage()
    const storage: ReloadMarkerStorage = { ...readable, setItem: boom }

    expect(
      decideOnChunkFailure({ error: chunkError, target: '/places', storage, now: 1_000 }),
    ).toEqual({ action: 'message' })
  })
})

describe('decideFullLoad', () => {
  it('reloads when the target differs from the current URL only by its fragment', () => {
    expect(
      decideFullLoad({ target: '/places#memo-7', current: { pathname: '/places', search: '' } }),
    ).toEqual({ kind: 'reload' })
  })

  it('reloads when the target is the current URL', () => {
    expect(
      decideFullLoad({
        target: '/search?q=trip',
        current: { pathname: '/search', search: '?q=trip' },
      }),
    ).toEqual({ kind: 'reload' })
  })

  it('assigns when the path differs', () => {
    expect(
      decideFullLoad({ target: '/settings', current: { pathname: '/places', search: '' } }),
    ).toEqual({ kind: 'assign', url: '/settings' })
  })

  it('assigns when only the query differs — that IS a document load', () => {
    expect(
      decideFullLoad({ target: '/search?q=trip', current: { pathname: '/search', search: '' } }),
    ).toEqual({ kind: 'assign', url: '/search?q=trip' })
  })
})

describe('scheduleUpdateChecks', () => {
  function scheduler() {
    let tick: (() => void) | undefined
    return {
      schedule: (check: () => void, everyMs: number) => {
        tick = check
        return everyMs
      },
      run: () => {
        if (!tick) throw new Error('nothing was scheduled')
        tick()
      },
    }
  }

  it('asks the registration for an update while the page is visible', () => {
    const update = vi.fn(() => Promise.resolve())
    const clock = scheduler()
    scheduleUpdateChecks({
      registration: { update },
      visibility: () => 'visible',
      schedule: clock.schedule,
    })

    clock.run()

    expect(update).toHaveBeenCalledTimes(1)
  })

  it('asks for nothing while the page is hidden', () => {
    const update = vi.fn(() => Promise.resolve())
    const clock = scheduler()
    scheduleUpdateChecks({
      registration: { update },
      visibility: () => 'hidden',
      schedule: clock.schedule,
    })

    clock.run()

    expect(update).not.toHaveBeenCalled()
  })

  it('swallows a failed update check instead of leaving a rejection unhandled', async () => {
    const rejection = new Error('Failed to fetch')
    const clock = scheduler()
    const unhandled: unknown[] = []
    const onUnhandled = (event: PromiseRejectionEvent) => {
      unhandled.push(event.reason)
    }
    window.addEventListener('unhandledrejection', onUnhandled)
    scheduleUpdateChecks({
      registration: { update: () => Promise.reject(rejection) },
      visibility: () => 'visible',
      schedule: clock.schedule,
    })

    expect(() => clock.run()).not.toThrow()
    await new Promise((resolve) => setTimeout(resolve, 10))
    window.removeEventListener('unhandledrejection', onUnhandled)

    expect(unhandled).toEqual([])
  })
})

describe('service-worker controller changes', () => {
  it('does not mark a full load when no controller existed before (fresh install)', () => {
    const state = noteControllerChange(initialBuildRefreshState(), { hadController: false })

    expect(shouldFullLoadOnNavigation(state)).toBe(false)
  })

  it('marks a full load when a controller was replaced (a new build took over)', () => {
    const state = noteControllerChange(initialBuildRefreshState(), { hadController: true })

    expect(shouldFullLoadOnNavigation(state)).toBe(true)
  })

  it('leaves the input state untouched', () => {
    const before = initialBuildRefreshState()
    noteControllerChange(before, { hadController: true })

    expect(shouldFullLoadOnNavigation(before)).toBe(false)
  })

  it('consumes the mark, so only the next navigation pays for the full load', () => {
    const marked = noteControllerChange(initialBuildRefreshState(), { hadController: true })
    const consumed = consumeFullLoad(marked)

    expect(shouldFullLoadOnNavigation(consumed)).toBe(false)
  })
})

describe('shouldCheckForUpdate', () => {
  it('polls while the document is visible', () => {
    expect(shouldCheckForUpdate({ visibility: 'visible' })).toBe(true)
  })

  it('does not poll a hidden document', () => {
    expect(shouldCheckForUpdate({ visibility: 'hidden' })).toBe(false)
  })

  it('polls on an hour-scale interval, not a minute-scale one', () => {
    expect(BUILD_UPDATE_POLL_MS).toBeGreaterThanOrEqual(30 * 60_000)
  })
})
