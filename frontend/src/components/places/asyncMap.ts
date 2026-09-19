import { type AsyncComponentLoader, defineAsyncComponent, defineComponent, h } from 'vue'

import MapPaneFallback from './MapPaneFallback.vue'

/**
 * The map pane, fetched on its own so the route does not have to wait for it: `/places` resolves
 * as fast as Explore does, and the pane carries the wait — and the failure — where the viewer is
 * already looking.
 *
 * `onRetry` has to hand back a NEW component rather than reload this one: `defineAsyncComponent`
 * memoises its loader's promise, a rejected one included, so the instance that failed can only
 * fail again.
 */
export function asyncMapComponent(loader: AsyncComponentLoader, onRetry: () => void) {
  return defineAsyncComponent({
    loader,
    loadingComponent: MapPaneFallback,
    errorComponent: defineComponent({
      name: 'MapPaneError',
      setup: () => () => h(MapPaneFallback, { failed: true, onRetry }),
    }),
    // The pane would otherwise sit empty for Vue's default 200 ms before admitting to the wait.
    delay: 0,
  })
}
