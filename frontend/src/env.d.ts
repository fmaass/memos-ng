/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

declare const __APP_VERSION__: string

declare module 'markdown-it-task-lists'

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
