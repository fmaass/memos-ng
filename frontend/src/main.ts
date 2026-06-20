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

createApp(App)
  .use(router)
  .use(VueQueryPlugin)
  .use(PrimeVue, {
    theme: {
      preset: Aura,
      options: {
        darkModeSelector: '.app-dark',
        cssLayer: { name: 'primevue', order: 'theme, base, primevue' },
      },
    },
  })
  .use(ToastService)
  .use(ConfirmationService)
  .mount('#app')
