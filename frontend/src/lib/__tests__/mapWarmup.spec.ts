import { describe, expect, it, vi } from 'vitest'

import {
  idleSchedule,
  MAP_WARMUP_DELAY_MS,
  runMapWarmup,
  scheduleMapWarmup,
  shouldWarmMap,
} from '@/lib/mapWarmup'

const idleBrowser = { online: true, saveData: false, routeName: 'home' }

describe('shouldWarmMap', () => {
  it('warms the map chunk on an idle browser that is elsewhere in the app', () => {
    expect(shouldWarmMap(idleBrowser)).toBe(true)
  })

  it('leaves a data-saver connection alone', () => {
    // The viewer has asked the browser to spend no bytes they did not ask for; a megabyte of
    // map they may never open is exactly that.
    expect(shouldWarmMap({ ...idleBrowser, saveData: true })).toBe(false)
  })

  it('does not warm while the browser is offline', () => {
    expect(shouldWarmMap({ ...idleBrowser, online: false })).toBe(false)
  })

  it('does not warm the chunk the open route is already loading', () => {
    expect(shouldWarmMap({ ...idleBrowser, routeName: 'places' })).toBe(false)
  })

  it('warms on a route it cannot name', () => {
    expect(shouldWarmMap({ ...idleBrowser, routeName: undefined })).toBe(true)
  })
})

describe('runMapWarmup', () => {
  it('loads the chunk when the environment allows it', async () => {
    const warm = vi.fn(() => Promise.resolve({}))
    await expect(runMapWarmup({ readEnvironment: () => idleBrowser, warm })).resolves.toBe('warmed')
    expect(warm).toHaveBeenCalledTimes(1)
  })

  it('asks for nothing at all when it may not warm', async () => {
    // Asserted where the decision is made: a skipped warm-up must not have touched the network.
    const warm = vi.fn(() => Promise.resolve({}))
    await expect(
      runMapWarmup({ readEnvironment: () => ({ ...idleBrowser, saveData: true }), warm }),
    ).resolves.toBe('skipped')
    expect(warm).not.toHaveBeenCalled()
  })

  it('reads the environment when it fires, not when it was scheduled', async () => {
    const readEnvironment = vi.fn(() => idleBrowser)
    await runMapWarmup({ readEnvironment, warm: () => Promise.resolve({}) })
    expect(readEnvironment).toHaveBeenCalledTimes(1)
  })

  it('swallows a failed warm-up', async () => {
    // It is an optimisation: a failure may not reach a toast, and may not leave an unhandled
    // rejection behind either.
    const warm = vi.fn(() => Promise.reject(new Error('chunk load failed')))
    await expect(runMapWarmup({ readEnvironment: () => idleBrowser, warm })).resolves.toBe('failed')
  })
})

describe('idleSchedule', () => {
  it('waits out the delay and then for the browser to go idle', () => {
    const ran: string[] = []
    let timeoutFn: (() => void) | null = null
    let idleFn: (() => void) | null = null
    const host = {
      setTimeout: (fn: () => void, ms: number) => {
        ran.push(`timeout:${ms}`)
        timeoutFn = fn
        return 1
      },
      requestIdleCallback: (fn: () => void) => {
        ran.push('idle')
        idleFn = fn
        return 2
      },
    }

    const fired = vi.fn()
    idleSchedule(host, fired, MAP_WARMUP_DELAY_MS)
    expect(ran).toEqual([`timeout:${MAP_WARMUP_DELAY_MS}`])
    expect(fired).not.toHaveBeenCalled()

    timeoutFn!()
    expect(ran).toEqual([`timeout:${MAP_WARMUP_DELAY_MS}`, 'idle'])
    expect(fired).not.toHaveBeenCalled()

    idleFn!()
    expect(fired).toHaveBeenCalledTimes(1)
  })

  it('fires on the timeout alone where there is no requestIdleCallback — every Safari', () => {
    let timeoutFn: (() => void) | null = null
    const host = {
      setTimeout: (fn: () => void) => {
        timeoutFn = fn
        return 1
      },
    }

    const fired = vi.fn()
    idleSchedule(host, fired, MAP_WARMUP_DELAY_MS)
    timeoutFn!()
    expect(fired).toHaveBeenCalledTimes(1)
  })
})

describe('scheduleMapWarmup', () => {
  it('runs the warm-up the scheduler hands it back, and not before', async () => {
    let scheduled: (() => void) | null = null
    const warm = vi.fn(() => Promise.resolve({}))
    scheduleMapWarmup({
      schedule: (fn) => {
        scheduled = fn
      },
      readEnvironment: () => idleBrowser,
      warm,
    })

    expect(warm).not.toHaveBeenCalled()
    scheduled!()
    expect(warm).toHaveBeenCalledTimes(1)
  })
})
