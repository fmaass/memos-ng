import { type AsyncComponentLoader, defineAsyncComponent, defineComponent, h } from 'vue'

import MapPaneFallback from './MapPaneFallback.vue'

/** What the pane can still offer after a failed load: one more attempt, or a new document. */
export type MapLoadRung = 'retry' | 'reload'

/**
 * How far down the ladder a pane that has failed `failures` times in this document has come.
 *
 * A browser that remembers a failed module fetch answers the identical URL from that memory
 * rather than the network, until the document is replaced — and which engines do that is nothing
 * this code can find out. So a retry gets one honest attempt, and after that the only rung left
 * that always reaches the network is a reload.
 */
export function mapLoadRung(failures: number): MapLoadRung {
  return failures >= 2 ? 'reload' : 'retry'
}

export interface MapPaneRecovery {
  /** Read when the failure is painted, so it reflects the attempt that just failed. */
  rung: () => MapLoadRung
  /** The viewer took the offer. */
  onAct: () => void
}

/**
 * The map pane, fetched on its own so the route does not have to wait for it: `/places` resolves
 * as fast as Explore does, and the pane carries the wait — and the failure — where the viewer is
 * already looking.
 *
 * A retry has to hand back a NEW component rather than reload this one: `defineAsyncComponent`
 * memoises its loader's promise, a rejected one included, so the instance that failed can only
 * fail again.
 */
export function asyncMapComponent(loader: AsyncComponentLoader, recovery: MapPaneRecovery) {
  return defineAsyncComponent({
    loader,
    loadingComponent: MapPaneFallback,
    errorComponent: defineComponent({
      name: 'MapPaneError',
      setup: () => () =>
        h(MapPaneFallback, { failed: true, rung: recovery.rung(), onRetry: recovery.onAct }),
    }),
    // The pane would otherwise sit empty for Vue's default 200 ms before admitting to the wait.
    delay: 0,
  })
}
