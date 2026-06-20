import { createRouter, createWebHistory } from 'vue-router'

import ArchivedView from './views/ArchivedView.vue'
import ExploreView from './views/ExploreView.vue'
import HomeView from './views/HomeView.vue'
import ResourcesView from './views/ResourcesView.vue'
import SearchView from './views/SearchView.vue'
import SettingsView from './views/SettingsView.vue'

// All routes render inside AppShell (App.vue). Auth is enforced at the API layer: any
// 401 from the client triggers a redirect to /api/auth/login (see api/client.ts), so a
// deep-linked route while logged out bounces to login and returns here afterwards.
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/explore', name: 'explore', component: ExploreView },
    { path: '/archived', name: 'archived', component: ArchivedView },
    { path: '/resources', name: 'resources', component: ResourcesView },
    { path: '/search', name: 'search', component: SearchView },
    { path: '/settings', name: 'settings', component: SettingsView },
  ],
})
