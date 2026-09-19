import { definePreset } from '@primeuix/themes'
import Aura from '@primeuix/themes/aura'
import { VueQueryPlugin } from '@tanstack/vue-query'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { createApp } from 'vue'

import { registerSW } from 'virtual:pwa-register'

import { installReachability } from './api/reachability'
import App from './App.vue'
import { BUILD_UPDATE_POLL_MS, shouldCheckForUpdate } from './lib/buildRefresh'
import { loadPlacesMap, recordStylesheetFailures } from './lib/places/loadMap'
import { idleSchedule, MAP_WARMUP_DELAY_MS, scheduleMapWarmup } from './lib/mapWarmup'
import router, { wireBuildRefresh } from './router'
import './style.css'
import 'primeicons/primeicons.css'

// Register the app-shell + Web Push service worker; autoUpdate swaps in new versions. A page left
// open across a deploy would otherwise never ask again — so while it is visible, ask hourly.
registerSW({
  immediate: true,
  onRegisteredSW(_swScriptUrl, registration) {
    if (!registration) return
    window.setInterval(() => {
      if (shouldCheckForUpdate({ visibility: document.visibilityState })) void registration.update()
    }, BUILD_UPDATE_POLL_MS)
  },
})

// A dead lazy route (its chunk gone with the old build) reloads once onto the current build, and a
// build that takes over mid-session is picked up on the next navigation — never mid-typing.
wireBuildRefresh({
  onError: (handler) => router.onError(handler),
  beforeEach: (guard) => router.beforeEach(guard),
  storage: sessionStorage,
  now: () => Date.now(),
  assign: (url) => window.location.assign(url),
  serviceWorker: 'serviceWorker' in navigator ? navigator.serviceWorker : undefined,
})

// Reachability drives TanStack Query's online state from here on, not `navigator.onLine`.
installReachability()

// Brand primary = indigo (matches the PWA manifest theme_color #6366f1); raw Aura
// otherwise defaults to emerald, which is off-brand.
const MemosPreset = definePreset(Aura, {
  semantic: {
    primary: {
      50: '{indigo.50}',
      100: '{indigo.100}',
      200: '{indigo.200}',
      300: '{indigo.300}',
      400: '{indigo.400}',
      500: '{indigo.500}',
      600: '{indigo.600}',
      700: '{indigo.700}',
      800: '{indigo.800}',
      900: '{indigo.900}',
      950: '{indigo.950}',
    },
  },
})

createApp(App)
  .use(router)
  .use(VueQueryPlugin)
  .use(PrimeVue, {
    // Weeks start on Monday (deep-merged into the default locale, so day/month names are kept).
    locale: { firstDayOfWeek: 1 },
    theme: {
      preset: MemosPreset,
      options: {
        darkModeSelector: '.app-dark',
        // order MUST match the @layer declaration in style.css (incl. utilities)
        cssLayer: { name: 'primevue', order: 'theme, base, primevue, utilities' },
      },
    },
  })
  .use(ToastService)
  .use(ConfirmationService)
  .mount('#app')

// Before anything can ask for a chunk: a stylesheet that fails is never fetched again by itself,
// and the warm-up below is the first thing that can burn one.
recordStylesheetFailures(document)

// The map chunk is excluded from the precache on purpose (a cold Home must not pay for it), which
// left every first tap on Places waiting for a megabyte. Fetch it once the app is up and the
// browser has nothing better to do — the decision itself is `shouldWarmMap`.
scheduleMapWarmup({
  schedule: (fn) => idleSchedule(window, fn, MAP_WARMUP_DELAY_MS),
  readEnvironment: () => ({
    online: navigator.onLine,
    saveData:
      (navigator as Navigator & { connection?: { saveData?: boolean } }).connection?.saveData ===
      true,
    routeName: router.currentRoute.value.name?.toString(),
  }),
  warm: loadPlacesMap,
})
