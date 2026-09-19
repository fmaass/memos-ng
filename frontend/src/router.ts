import ToastEventBus from 'primevue/toasteventbus'
import { createRouter, createWebHistory } from 'vue-router'

import {
  consumeFullLoad,
  decideFullLoad,
  decideOnChunkFailure,
  type CurrentLocation,
  initialBuildRefreshState,
  NEW_BUILD_MESSAGE,
  noteControllerChange,
  shouldFullLoadOnNavigation,
  type ReloadMarkerStorage,
} from './lib/buildRefresh'
import HomeView from './views/HomeView.vue'

// Home (the feed) is the cold-open route, so it's eager — everything else is lazy-loaded
// (`() => import`) into its own chunk, fetched only on navigation. This keeps the initial bundle
// small: opening the app no longer downloads/parses Resources (justified gallery), Settings,
// Search, and Explore. All routes render inside AppShell (App.vue). Auth is enforced at the API
// layer: any 401 from the client triggers a redirect to /api/auth/login (see api/client.ts).
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/explore', name: 'explore', component: () => import('./views/ExploreView.vue') },
    // The map chunk (MapLibre + pmtiles + the basemap layers) is the heaviest route in the app:
    // lazy like the rest, so only opening Places pays for it.
    { path: '/places', name: 'places', component: () => import('./views/PlacesView.vue') },
    { path: '/archived', name: 'archived', component: () => import('./views/ArchivedView.vue') },
    { path: '/resources', name: 'resources', component: () => import('./views/ResourcesView.vue') },
    { path: '/search', name: 'search', component: () => import('./views/SearchView.vue') },
    { path: '/settings', name: 'settings', component: () => import('./views/SettingsView.vue') },
    // The canonical URL of one memo — what the app shares out and what a phone without the app
    // installed falls back to (the /m/* pattern the AASA applinks document claims). Lazy like the
    // rest: a deep-linked cold open pays for this chunk, the feed's opening does not.
    { path: '/m/:uid', name: 'memo', component: () => import('./views/MemoView.vue') },
  ],
})

/** Everything the wiring touches, narrowed so a test can drive the two hooks by hand. */
export interface ChunkErrorTarget {
  /** The route path alone — the marker's identity, so `?q=1` and `#x` share one guard. */
  path: string
  /** What a full load has to land on, query and fragment included. */
  fullPath: string
}

export interface ServiceWorkerLike {
  controller: unknown
  addEventListener(type: 'controllerchange', handler: () => void): void
}

export interface BuildRefreshWiring {
  onError(handler: (error: unknown, to: ChunkErrorTarget) => void): unknown
  beforeEach(guard: (to: ChunkErrorTarget) => boolean | void): unknown
  storage: ReloadMarkerStorage
  now(): number
  /** Where the document is, read at the moment a full load is decided. */
  currentLocation(): CurrentLocation
  /** A FULL load, not a router push: the point is to fetch the new build's index.html. */
  assign(url: string): void
  /** The full load for a target the browser would treat as the same document. */
  reload(): void
  serviceWorker?: ServiceWorkerLike | undefined
  /** Injection seam, so a test can prove which paths reach the decision at all. */
  decide?: typeof decideOnChunkFailure | undefined
}

/**
 * Connects the decisions in `lib/buildRefresh` to the router and the service worker. The router's
 * error hook is the ONLY reload trigger: a `vite:preloadError` listener would also catch the idle
 * map warm-up, which is a speculative fetch nobody is waiting for and must never reload the page.
 */
export function wireBuildRefresh(deps: BuildRefreshWiring): void {
  const decide = deps.decide ?? decideOnChunkFailure
  let state = initialBuildRefreshState()
  // The document is already on its way out; a second navigation from the other path would only
  // race the first. An import that rejects after the guard has fired lands here too.
  let loadRequested = false

  function fullLoad(target: string): void {
    if (loadRequested) return
    loadRequested = true
    const plan = decideFullLoad({ target, current: deps.currentLocation() })
    if (plan.kind === 'assign') deps.assign(plan.url)
    else deps.reload()
  }

  deps.onError((error, to) => {
    const { action } = decide({
      error,
      target: to.path,
      storage: deps.storage,
      now: deps.now(),
    })
    if (action === 'reload') fullLoad(to.fullPath)
    // `useToast` needs a component instance; the bus it writes to does not, and this runs from
    // main.ts. ToastService (installed on the app) is the reader on the other end.
    else if (action === 'message') {
      ToastEventBus.emit('add', {
        severity: 'warn',
        summary: 'Update available',
        detail: NEW_BUILD_MESSAGE,
      })
    }
  })

  deps.beforeEach((to) => {
    if (!shouldFullLoadOnNavigation(state)) return
    state = consumeFullLoad(state)
    fullLoad(to.fullPath)
    // The full load replaces this document; letting the SPA navigation also run would only
    // render a screen from the old build for the moment in between.
    return false
  })

  const serviceWorker = deps.serviceWorker
  if (!serviceWorker) return
  // Read at wiring time: by the time `controllerchange` fires, `controller` is already the new
  // worker, so it can no longer answer whether one was there before.
  let hadController = serviceWorker.controller != null
  serviceWorker.addEventListener('controllerchange', () => {
    state = noteControllerChange(state, { hadController })
    hadController = true
  })
}
