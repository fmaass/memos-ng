import { definePreset } from '@primeuix/themes'
import Aura from '@primeuix/themes/aura'
import { VueQueryPlugin } from '@tanstack/vue-query'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { createApp } from 'vue'

import { registerSW } from 'virtual:pwa-register'

import App from './App.vue'
import router from './router'
import './style.css'
import 'primeicons/primeicons.css'

// Register the app-shell + Web Push service worker; autoUpdate swaps in new versions.
registerSW({ immediate: true })

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
