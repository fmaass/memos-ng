/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

declare const __APP_VERSION__: string

declare module 'markdown-it-task-lists'

// The PWA plugin's registration helper carries no types of its own (the typed entry point is the
// virtual module the build generates from it). Imported by name only in the test that drives the
// helper's own reload path.
declare module '*/vite-plugin-pwa/dist/client/build/register.js' {
  import type { RegisterSWOptions } from 'vite-plugin-pwa/types'

  export function registerSW(options?: RegisterSWOptions): (reloadPage?: boolean) => Promise<void>
}

// PrimeVue ships no declaration for the toast bus (`primevue/toasteventbus` is the one entry
// point without an index.d.ts), and it is the only toast path that works outside a component —
// `ToastService.add()` is a one-line wrapper around exactly this emit.
declare module 'primevue/toasteventbus' {
  import type { ToastMessageOptions } from 'primevue/toast'

  const ToastEventBus: {
    emit(event: 'add' | 'remove' | 'remove-group' | 'remove-all-groups', payload?: unknown): void
    on(event: 'add', handler: (message: ToastMessageOptions) => void): void
    off(event: 'add', handler: (message: ToastMessageOptions) => void): void
  }
  export default ToastEventBus
}
