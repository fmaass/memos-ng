import { describe, expect, it } from 'vitest'

import {
  DOCK_SEGMENT_THRESHOLD as T,
  type DockState,
  dockReducer,
  initialDockState,
} from '@/lib/dockState'

const tabs: DockState = { mode: 'tabs' }
const segment: DockState = { mode: 'segment' }
const LIB = '/resources'

describe('dockReducer', () => {
  it('starts in tabs', () => {
    expect(initialDockState()).toEqual(tabs)
  })

  it('enters segment the moment the Library is scrolled past the threshold', () => {
    expect(dockReducer(tabs, { type: 'scroll', y: T + 1, route: LIB })).toEqual(segment)
    expect(dockReducer(segment, { type: 'scroll', y: 5000, route: LIB })).toEqual(segment)
  })

  it('returns to tabs at the top of the Library', () => {
    expect(dockReducer(segment, { type: 'scroll', y: 0, route: LIB })).toEqual(tabs)
    // iOS rubber-banding reports a negative scrollTop at the top.
    expect(dockReducer(segment, { type: 'scroll', y: -40, route: LIB })).toEqual(tabs)
  })

  it('leaves the mode unchanged between the top and the threshold', () => {
    for (const y of [1, T / 2, T]) {
      expect(dockReducer(tabs, { type: 'scroll', y, route: LIB })).toBe(tabs)
      expect(dockReducer(segment, { type: 'scroll', y, route: LIB })).toBe(segment)
    }
  })

  it('toggles on a tap of the active Library tab', () => {
    expect(dockReducer(tabs, { type: 'tapActive', route: LIB })).toEqual(segment)
    expect(dockReducer(segment, { type: 'tapActive', route: LIB })).toEqual(tabs)
  })

  it('goes back to segment on the next scroll past the threshold after a mid-page tap', () => {
    // Intended: a tap expands the tabs until the next scroll (plan decision 8).
    let s = dockReducer(tabs, { type: 'scroll', y: 800, route: LIB })
    s = dockReducer(s, { type: 'tapActive', route: LIB })
    expect(s).toEqual(tabs)
    s = dockReducer(s, { type: 'scroll', y: 820, route: LIB })
    expect(s).toEqual(segment)
  })

  it('resets to tabs on leaving the Library', () => {
    expect(dockReducer(segment, { type: 'leaveLibrary' })).toEqual(tabs)
    expect(dockReducer(tabs, { type: 'leaveLibrary' })).toEqual(tabs)
  })

  it.each(['/', '/places', '/resources/x', '/search'])(
    'returns the state unchanged for a scroll or a tap observed on %s',
    (route) => {
      for (const state of [tabs, segment]) {
        expect(dockReducer(state, { type: 'scroll', y: 5000, route })).toBe(state)
        expect(dockReducer(state, { type: 'scroll', y: 0, route })).toBe(state)
        expect(dockReducer(state, { type: 'tapActive', route })).toBe(state)
      }
    },
  )
})
