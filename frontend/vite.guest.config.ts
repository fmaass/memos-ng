import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import { guestIsolation } from './vite-plugins/guestIsolation'

// The guest page of a Trip Pass (trip pass P4): its own entry, its own output, served by the
// ingest door alone (`/p/{secret}`, `/p/joined`, `/guest-assets/*` — app/ingest/guest.py). It
// shares no plugin with the member build: no PWA, no Tailwind, no router — and the isolation
// plugin proves at build time that no member-app module reaches its graph or its worker's.
const root = fileURLToPath(new URL('.', import.meta.url))
const isolation = (mustSee: string) => guestIsolation({ root, mustSee })

export default defineConfig({
  root,
  // The member app's public/ (icons, manifest) is not the guest page's.
  publicDir: false,
  plugins: [vue(), isolation(`${root}src/guest/main.ts`)],
  build: {
    outDir: 'dist-guest',
    // The one prefix the door serves files under.
    assetsDir: 'guest-assets',
    emptyOutDir: true,
    // The polyfill is injected as a script of its own; the policy admits no inline script.
    modulePreload: { polyfill: false },
    rollupOptions: { input: { guest: `${root}guest.html` } },
  },
  worker: {
    // One self-contained file per worker: nothing for the worker to import at run time.
    format: 'iife',
    plugins: () => [isolation(`${root}src/guest/hashWorker.ts`)],
  },
})
