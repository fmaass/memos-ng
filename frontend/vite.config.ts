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
      registerType: 'autoUpdate',
      injectManifest: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2}'],
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
