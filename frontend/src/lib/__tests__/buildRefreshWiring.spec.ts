import ToastEventBus from 'primevue/toasteventbus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { decideOnChunkFailure, type ReloadMarkerStorage } from '../buildRefresh'
import { runMapWarmup } from '../mapWarmup'
import { wireBuildRefresh, type ChunkErrorTarget } from '../../router'

const CHUNK_ERROR = new TypeError(
  'Failed to fetch dynamically imported module: /assets/PlacesView-a1b2c3.js',
)

function fakeStorage(): ReloadMarkerStorage {
  const entries = new Map<string, string>()
  return {
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => {
      entries.set(key, value)
    },
    removeItem: (key) => {
      entries.delete(key)
    },
  }
}

/** vue-router's `path` is the `fullPath` without its query string and fragment. */
function asTarget(fullPath: string): ChunkErrorTarget {
  return { path: fullPath.split(/[?#]/)[0]!, fullPath }
}

/** The two router hooks the wiring installs, driven by hand — no mounted app, no real routing. */
function fakeRouter() {
  let onErrorHandler: ((error: unknown, to: ChunkErrorTarget) => void) | undefined
  let guard: ((to: ChunkErrorTarget) => boolean | void) | undefined
  return {
    onError(handler: (error: unknown, to: ChunkErrorTarget) => void) {
      onErrorHandler = handler
    },
    beforeEach(next: (to: ChunkErrorTarget) => boolean | void) {
      guard = next
    },
    fail(error: unknown, fullPath: string) {
      if (!onErrorHandler) throw new Error('wiring installed no router error handler')
      onErrorHandler(error, asTarget(fullPath))
    },
    navigate(fullPath: string) {
      if (!guard) throw new Error('wiring installed no navigation guard')
      return guard(asTarget(fullPath))
    },
  }
}

function fakeServiceWorker(controller: object | null) {
  const handlers: Array<() => void> = []
  return {
    controller,
    addEventListener(_type: 'controllerchange', handler: () => void) {
      handlers.push(handler)
    },
    takeOver() {
      for (const handler of handlers) handler()
    },
  }
}

function wire(overrides: {
  router: ReturnType<typeof fakeRouter>
  assign: (url: string) => void
  reload?: () => void
  storage?: ReloadMarkerStorage
  currentLocation?: () => { pathname: string; search: string }
  serviceWorker?: ReturnType<typeof fakeServiceWorker>
  decide?: typeof decideOnChunkFailure
  now?: () => number
}) {
  wireBuildRefresh({
    onError: overrides.router.onError,
    beforeEach: overrides.router.beforeEach,
    storage: overrides.storage ?? fakeStorage(),
    now: overrides.now ?? (() => 1_000),
    currentLocation: overrides.currentLocation ?? (() => ({ pathname: '/', search: '' })),
    assign: overrides.assign,
    reload: overrides.reload ?? (() => undefined),
    serviceWorker: overrides.serviceWorker,
    decide: overrides.decide,
  })
}

describe('wireBuildRefresh', () => {
  const toasts: Array<Record<string, unknown>> = []
  const capture = (message: unknown) => {
    toasts.push(message as Record<string, unknown>)
  }

  beforeEach(() => {
    toasts.length = 0
    ToastEventBus.on('add', capture)
  })

  afterEach(() => {
    ToastEventBus.off('add', capture)
  })

  it('reloads onto the current build when a route chunk fails', () => {
    const assign = vi.fn()
    const router = fakeRouter()
    wire({ router, assign })

    router.fail(CHUNK_ERROR, '/places')

    expect(assign).toHaveBeenCalledWith('/places')
    expect(toasts).toHaveLength(0)
  })

  it('reloads exactly once and then shows an actionable message (the loop guard)', () => {
    const assign = vi.fn()
    const router = fakeRouter()
    let clock = 1_000
    wire({ router, assign, now: () => clock })

    router.fail(CHUNK_ERROR, '/places')
    clock += 2_000
    router.fail(CHUNK_ERROR, '/places')

    expect(assign).toHaveBeenCalledTimes(1)
    expect(toasts).toHaveLength(1)
    expect(toasts[0]!.detail).toBe('A new version is available — reload the page')
  })

  it('leaves a non-chunk router error to the default handling', () => {
    const assign = vi.fn()
    const router = fakeRouter()
    wire({ router, assign })

    router.fail(new Error('memo 42 not found'), '/m/42')

    expect(assign).not.toHaveBeenCalled()
    expect(toasts).toHaveLength(0)
  })

  it('never reloads at the moment a new build takes over', () => {
    const assign = vi.fn()
    const router = fakeRouter()
    const serviceWorker = fakeServiceWorker({})
    wire({ router, assign, serviceWorker })

    serviceWorker.takeOver()

    expect(assign).not.toHaveBeenCalled()
  })

  it('performs the deferred full load on the next navigation, once', () => {
    const assign = vi.fn()
    const router = fakeRouter()
    const serviceWorker = fakeServiceWorker({})
    wire({ router, assign, serviceWorker })

    serviceWorker.takeOver()
    const first = router.navigate('/settings')
    const second = router.navigate('/search')

    expect(assign).toHaveBeenCalledTimes(1)
    expect(assign).toHaveBeenCalledWith('/settings')
    expect(first).toBe(false)
    expect(second).toBeUndefined()
  })

  it("does not full-load after a fresh install's first controllerchange", () => {
    const assign = vi.fn()
    const router = fakeRouter()
    const serviceWorker = fakeServiceWorker(null)
    wire({ router, assign, serviceWorker })

    serviceWorker.takeOver()
    router.navigate('/settings')

    expect(assign).not.toHaveBeenCalled()
  })

  it('full-loads on a later update even though the first take-over was the install', () => {
    const assign = vi.fn()
    const router = fakeRouter()
    const serviceWorker = fakeServiceWorker(null)
    wire({ router, assign, serviceWorker })

    serviceWorker.takeOver()
    serviceWorker.takeOver()
    router.navigate('/settings')

    expect(assign).toHaveBeenCalledWith('/settings')
  })

  it('installs, and still recovers, when every storage access throws', () => {
    const assign = vi.fn()
    const reload = vi.fn()
    const router = fakeRouter()
    const blocked: ReloadMarkerStorage = {
      getItem: () => {
        throw new DOMException('The operation is insecure.', 'SecurityError')
      },
      setItem: () => {
        throw new DOMException('The operation is insecure.', 'SecurityError')
      },
      removeItem: () => {
        throw new DOMException('The operation is insecure.', 'SecurityError')
      },
    }

    expect(() => wire({ router, assign, reload, storage: blocked })).not.toThrow()
    expect(() => router.fail(CHUNK_ERROR, '/places')).not.toThrow()

    expect(assign).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
    expect(toasts).toHaveLength(1)
  })

  it('reloads in place when the target only differs from the current URL by its fragment', () => {
    const assign = vi.fn()
    const reload = vi.fn()
    const router = fakeRouter()
    wire({ router, assign, reload, currentLocation: () => ({ pathname: '/places', search: '' }) })

    router.fail(CHUNK_ERROR, '/places#memo-7')

    expect(reload).toHaveBeenCalledTimes(1)
    expect(assign).not.toHaveBeenCalled()
  })

  it('guards URL variants of one route with a single marker', () => {
    const assign = vi.fn()
    const reload = vi.fn()
    const router = fakeRouter()
    let clock = 1_000
    wire({ router, assign, reload, now: () => clock })

    router.fail(CHUNK_ERROR, '/places?tag=trip')
    clock += 2_000
    router.fail(CHUNK_ERROR, '/places#memo-7')

    expect(assign).toHaveBeenCalledTimes(1)
    expect(assign).toHaveBeenCalledWith('/places?tag=trip')
    expect(toasts).toHaveLength(1)
  })

  it('issues one navigation when the chunk path and the deferred load both fire', () => {
    const assign = vi.fn()
    const reload = vi.fn()
    const router = fakeRouter()
    const serviceWorker = fakeServiceWorker({})
    wire({ router, assign, reload, serviceWorker })

    router.fail(CHUNK_ERROR, '/places')
    serviceWorker.takeOver()
    router.navigate('/settings')

    expect(assign.mock.calls.length + reload.mock.calls.length).toBe(1)
    expect(assign).toHaveBeenCalledWith('/places')
  })

  it('never reloads the page itself when a new build takes over', () => {
    const assign = vi.fn()
    const reload = vi.fn()
    const router = fakeRouter()
    const serviceWorker = fakeServiceWorker({})
    wire({ router, assign, reload, serviceWorker })

    serviceWorker.takeOver()

    expect(reload).not.toHaveBeenCalled()
    expect(assign).not.toHaveBeenCalled()
  })

  it("leaves the PWA plugin's own immediate reload out of the build", () => {
    // `registerType: 'autoUpdate'` makes vite-plugin-pwa's registration helper call
    // window.location.reload() on activation (dist/client/build/register.js:38-45) — no unit test
    // of ours can reach that virtual module, so the configuration is what gets pinned.
    const config = readFileSync(resolve(process.cwd(), 'vite.config.ts'), 'utf8')

    expect(config).toContain("registerType: 'prompt'")
    expect(config).not.toContain("registerType: 'autoUpdate'")
  })

  it('never routes a failed idle map warm-up into the chunk-failure decision', async () => {
    const assign = vi.fn()
    const router = fakeRouter()
    const decide = vi.fn(decideOnChunkFailure)
    wire({ router, assign, decide })

    const outcome = await runMapWarmup({
      readEnvironment: () => ({ online: true, saveData: false, routeName: 'home' }),
      warm: () => Promise.reject(CHUNK_ERROR),
    })

    expect(outcome).toBe('failed')
    expect(decide).not.toHaveBeenCalled()
    expect(assign).not.toHaveBeenCalled()
    expect(toasts).toHaveLength(0)

    // Positive control for the assertion above: the same spy, through the router's error hook,
    // DOES record the identical error — so "not called" means the warm-up never got there.
    router.fail(CHUNK_ERROR, '/places')
    expect(decide).toHaveBeenCalledTimes(1)
  })
})
