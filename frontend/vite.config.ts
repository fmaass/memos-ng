import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'
import { defineConfig } from 'vitest/config'

// Dev proxy target. Default 8000 (prod convention); override on hosts where 8000
// is taken — e.g. Merkur: VITE_API_PROXY=http://localhost:8011.
const apiTarget = process.env.VITE_API_PROXY || 'http://localhost:8000'

export default defineConfig({
  plugins: [
    vue(),
    tailwindcss(),
    VitePWA({
      // Custom SW (src/sw.ts) so it doubles as the Web Push receiver; vite-plugin-pwa
      // injects the precache manifest. Only built shell assets are precached — /api/* and
      // /file/* never match the glob, so API/media requests always hit the network.
      strategies: 'injectManifest',
      srcDir: 'src',
      filename: 'sw.ts',
      // NOT 'autoUpdate': in that mode the plugin's own registration helper reloads the page the
      // moment a new worker activates (node_modules/vite-plugin-pwa/dist/client/build/register.js
      // lines 38-45, `if (auto) … window.location.reload()`), which throws away whatever is being
      // typed. The hand-written worker still calls skipWaiting()/clientsClaim(), so the new build
      // takes control either way; when to actually load it is decided in `lib/buildRefresh`.
      registerType: 'prompt',
      injectManifest: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2}'],
        // The map chunk and MapLibre's worker are ~1 MB that only /places needs. Precaching them
        // would download the whole map on a cold Home, which is exactly what the lazy route
        // avoids — both are emitted as `map-*.js` (see `worker` and `manualChunks` below).
        // maplibre's stylesheet lands in the same chunk and is named the same way, so both
        // extensions are named here: the point is that a cold Home downloads NO map asset.
        globIgnores: ['**/map-*.js', '**/map-*.css'],
      },
      manifest: {
        name: 'memos-ng',
        short_name: 'memos',
        description: 'Memos rebuilt on the household default web stack',
        theme_color: '#6366f1',
        background_color: '#18181b',
        display: 'standalone',
        start_url: '/',
        icons: [
          { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png' },
          {
            src: '/icons/maskable-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      devOptions: { enabled: false },
    }),
  ],
  define: {
    __APP_VERSION__: JSON.stringify(process.env.VITE_APP_VERSION || 'dev'),
  },
  build: {
    rollupOptions: {
      output: {
        // Split heavy vendors into their own chunks so they download in parallel and cache
        // independently of app code (they change rarely). With lazy routes, this keeps the
        // cold-open critical path small.
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return
          if (/maplibre-gl|pmtiles|@protomaps|@mapbox|@maplibre/.test(id)) return 'map'
          if (/primevue|@primeuix|primeicons/.test(id)) return 'primevue'
          if (/markdown-it|dompurify|entities|linkifyjs|mdurl|uc\.micro|punycode/.test(id))
            return 'markdown'
          if (/[\\/]vue[\\/]|vue-router|@vue[\\/]|@tanstack/.test(id)) return 'vue'
          return 'vendor'
        },
      },
    },
  },
  // MapLibre v6 loads its worker from a URL of its own; naming that bundle `map-*` puts it under
  // the same precache exclusion as the map chunk.
  worker: {
    format: 'es',
    rollupOptions: { output: { entryFileNames: 'assets/map-worker-[hash].js' } },
  },
  resolve: {
    alias: { '@': new URL('./src', import.meta.url).pathname },
  },
  server: {
    proxy: {
      '/api': apiTarget,
      '/file': apiTarget,
      '/health': apiTarget,
    },
  },
  test: {
    environment: 'happy-dom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
  },
})
