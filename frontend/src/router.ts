import { createRouter, createWebHistory } from 'vue-router'

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
