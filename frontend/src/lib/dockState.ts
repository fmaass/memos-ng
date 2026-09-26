// The app dock's state rule (plan decision 8). Outside the Library the dock is the tab pill and
// nothing here changes it; inside, it is either the tabs (at the top, or after a tap on the active
// Library tab) or the Library segment (once the page is scrolled past a small threshold, until it
// is back at the top). Every event carries the route it was observed on, which is what makes
// "outside the Library nothing changes" decidable here rather than in a component. No idle timer
// and no scroll-direction test. An event that changes nothing returns the SAME state object, so a
// caller can skip the write.
export type DockMode = 'tabs' | 'segment'

export interface DockState {
  mode: DockMode
}

export type DockEvent =
  /** `y` is the scroll host's scrollTop — the document's, `document.scrollingElement`. */
  | { type: 'scroll'; y: number; route: string }
  | { type: 'tapActive'; route: string }
  | { type: 'leaveLibrary' }

export const LIBRARY_ROUTE = '/resources'

/** How far the Library scrolls before the segment replaces the tabs, in CSS px. */
export const DOCK_SEGMENT_THRESHOLD = 24

export function initialDockState(): DockState {
  return { mode: 'tabs' }
}

function withMode(state: DockState, mode: DockMode): DockState {
  return state.mode === mode ? state : { mode }
}

export function dockReducer(state: DockState, event: DockEvent): DockState {
  switch (event.type) {
    case 'leaveLibrary':
      return withMode(state, 'tabs')
    case 'tapActive':
      if (event.route !== LIBRARY_ROUTE) return state
      return withMode(state, state.mode === 'tabs' ? 'segment' : 'tabs')
    case 'scroll':
      if (event.route !== LIBRARY_ROUTE) return state
      // "At the top" is y <= 0, not y < threshold: rubber-banding reports negative values.
      if (event.y <= 0) return withMode(state, 'tabs')
      if (event.y > DOCK_SEGMENT_THRESHOLD) return withMode(state, 'segment')
      return state
  }
}
