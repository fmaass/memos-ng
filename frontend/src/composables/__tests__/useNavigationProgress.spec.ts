import { defineComponent, effectScope, h, type Ref } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useNavigationProgress } from '@/composables/useNavigationProgress'
import { NAV_PROGRESS_DELAY_MS } from '@/lib/navigationProgress'

// The real router, because the load-bearing premise is vue-router's own: a navigation that is
// superseded reports back through `afterEach` WITH a failure, and a component that fails to load
// reports through `onError` and never reaches `afterEach` at all.
const Blank = defineComponent({ setup: () => () => h('div') })

let resolveSlow: ((component: unknown) => void) | null = null
let resolveOther: ((component: unknown) => void) | null = null
let rejectBroken: ((error: unknown) => void) | null = null

function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'home', component: Blank },
      { path: '/fast', name: 'fast', component: Blank },
      {
        path: '/slow',
        name: 'slow',
        component: () =>
          new Promise((resolve) => {
            resolveSlow = resolve
          }),
      },
      {
        path: '/other',
        name: 'other',
        component: () =>
          new Promise((resolve) => {
            resolveOther = resolve
          }),
      },
      {
        path: '/broken',
        name: 'broken',
        component: () =>
          new Promise((_resolve, reject) => {
            rejectBroken = reject
          }),
      },
    ],
  })
}

/** Microtasks only: the router's guard queue is a promise chain, nothing in it is timed. */
async function flush() {
  for (let i = 0; i < 200; i++) await Promise.resolve()
}

let scope: ReturnType<typeof effectScope>
let router: Router
let visible: Ref<boolean>

beforeEach(async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  resolveSlow = null
  resolveOther = null
  rejectBroken = null
  router = makeRouter()
  scope = effectScope()
  visible = scope.run(() => useNavigationProgress(router))!
  await router.push('/')
  await flush()
  vi.advanceTimersByTime(10_000)
})

afterEach(() => {
  scope.stop()
  vi.useRealTimers()
})

describe('useNavigationProgress', () => {
  it('leaves an already-loaded route alone', async () => {
    visible.value = false
    await router.push('/fast')
    vi.advanceTimersByTime(10_000)

    expect(visible.value).toBe(false)
  })

  it('announces a navigation that is still waiting for its chunk', async () => {
    const navigation = router.push('/slow')
    await flush()
    expect(visible.value).toBe(false)

    vi.advanceTimersByTime(NAV_PROGRESS_DELAY_MS)
    expect(visible.value).toBe(true)

    resolveSlow!(Blank)
    await navigation
    await flush()
    expect(visible.value).toBe(false)
  })

  it('clears the bar when the chunk never arrives', async () => {
    const errors: unknown[] = []
    router.onError((error) => errors.push(error))
    void router.push('/broken').catch(() => {})
    await flush()
    vi.advanceTimersByTime(NAV_PROGRESS_DELAY_MS)
    expect(visible.value).toBe(true)

    rejectBroken!(new Error('chunk load failed'))
    await flush()

    expect(errors).toHaveLength(1)
    expect(visible.value).toBe(false)
  })

  it('keeps the bar up when a second navigation supersedes the first', async () => {
    void router.push('/slow').catch(() => {})
    await flush()
    vi.advanceTimersByTime(NAV_PROGRESS_DELAY_MS)
    expect(visible.value).toBe(true)

    const second = router.push('/broken')
    await flush()

    // The first navigation now resumes, finds itself cancelled and reports back — while the
    // second is still out. Its report may not take the second one's bar down with it.
    resolveSlow!(Blank)
    await flush()
    expect(visible.value).toBe(true)

    rejectBroken!(new Error('and this one fails too'))
    await second.catch(() => {})
    await flush()
    expect(visible.value).toBe(false)
  })

  it('keeps the bar up for the newest tap after a cached route arrived between them', async () => {
    // Resources (slow chunk), Explore (cached, arrives), Settings (slow chunk), and only then the
    // Resources chunk lands and reports itself cancelled — the order a phone on a slow tunnel
    // produces every time. The bar belongs to Settings until Settings says otherwise.
    void router.push('/slow').catch(() => {})
    await flush()
    await router.push('/fast')
    await flush()
    void router.push('/other').catch(() => {})
    await flush()
    vi.advanceTimersByTime(NAV_PROGRESS_DELAY_MS)
    expect(visible.value).toBe(true)

    resolveSlow!(Blank)
    await flush()
    expect(visible.value).toBe(true)

    resolveOther!(Blank)
    await flush()
    expect(visible.value).toBe(false)
  })

  it('takes the bar down when the newest tap fails, however long an older one stalls', async () => {
    // The mirror case: nothing will ever come back from the abandoned first navigation, so a bar
    // that waits for it claims the app is still on its way somewhere it has given up on.
    void router.push('/slow').catch(() => {})
    await flush()
    void router.push('/broken').catch(() => {})
    await flush()
    vi.advanceTimersByTime(NAV_PROGRESS_DELAY_MS)
    expect(visible.value).toBe(true)

    rejectBroken!(new Error('chunk load failed'))
    await flush()
    expect(visible.value).toBe(false)
  })

  it('hands beforeEach, afterEach and onError one and the same location object', async () => {
    // The identity the bar is keyed on. vue-router builds `toLocation` once per navigation and
    // passes that object to every hook — a claim this composable would silently fail without.
    const before: unknown[] = []
    const after: unknown[] = []
    const failed: unknown[] = []
    router.beforeEach((to) => {
      before.push(to)
    })
    router.afterEach((to) => {
      after.push(to)
    })
    router.onError((_error, to) => {
      failed.push(to)
    })

    await router.push('/fast')
    await flush()
    expect(after[0]).toBe(before[0])

    void router.push('/broken').catch(() => {})
    await flush()
    rejectBroken!(new Error('chunk load failed'))
    await flush()
    expect(failed[0]).toBe(before[1])
  })

  it('unhooks itself from the router when its scope is disposed', async () => {
    scope.stop()
    void router.push('/slow').catch(() => {})
    await flush()
    vi.advanceTimersByTime(10_000)

    expect(visible.value).toBe(false)
  })
})
