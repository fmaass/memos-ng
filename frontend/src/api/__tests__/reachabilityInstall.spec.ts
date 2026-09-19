import { onlineManager, QueryClient } from '@tanstack/vue-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { installReachability } from '@/api/reachability'

describe('installReachability', () => {
  afterEach(() => vi.restoreAllMocks())

  it('wires the browser once, and takes query pausing off navigator.onLine', () => {
    // Subscribing first makes TanStack Query install its own window online/offline listeners,
    // which is exactly what the install has to take away: `navigator.onLine` stays true through
    // the failure this whole feature exists for, and would be a liar in both directions.
    const unsubscribe = onlineManager.subscribe(() => {})
    const add = vi.spyOn(window, 'addEventListener')

    installReachability(new QueryClient())
    installReachability(new QueryClient())

    // Twice called, once wired — a second install must add no listener and reset no state.
    expect(add.mock.calls.filter(([type]) => type === 'online')).toHaveLength(1)

    window.dispatchEvent(new Event('offline'))
    expect(onlineManager.isOnline()).toBe(true)

    unsubscribe()
  })
})
