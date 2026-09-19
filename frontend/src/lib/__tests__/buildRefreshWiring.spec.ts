import ToastEventBus from 'primevue/toasteventbus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { decideOnChunkFailure, type ReloadMarkerStorage } from '../buildRefresh'
import { runMapWarmup } from '../mapWarmup'
import { wireBuildRefresh } from '../../router'

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

/** The two router hooks the wiring installs, driven by hand — no mounted app, no real routing. */
function fakeRouter() {
  let onErrorHandler: ((error: unknown, to: { fullPath: string }) => void) | undefined
  let guard: ((to: { fullPath: string }) => boolean | void) | undefined
  return {
    onError(handler: (error: unknown, to: { fullPath: string }) => void) {
      onErrorHandler = handler
    },
    beforeEach(next: (to: { fullPath: string }) => boolean | void) {
      guard = next
    },
    fail(error: unknown, fullPath: string) {
      if (!onErrorHandler) throw new Error('wiring installed no router error handler')
      onErrorHandler(error, { fullPath })
    },
    navigate(fullPath: string) {
      if (!guard) throw new Error('wiring installed no navigation guard')
      return guard({ fullPath })
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
  serviceWorker?: ReturnType<typeof fakeServiceWorker>
  decide?: typeof decideOnChunkFailure
  now?: () => number
}) {
  wireBuildRefresh({
    onError: overrides.router.onError,
    beforeEach: overrides.router.beforeEach,
    storage: fakeStorage(),
    now: overrides.now ?? (() => 1_000),
    assign: overrides.assign,
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
