import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

// HESM dashboard — Vite + React + TypeScript + PWA service worker.
// Built artifacts go to dist/ and are deployed to Netlify.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      // Auto-update keeps the user on the latest deploy without prompting.
      registerType: 'autoUpdate',

      // Static assets bundled with the precache. We deliberately exclude
      // index.html — it's already cached as the navigation fallback.
      includeAssets: ['icon.svg', 'manifest.json'],

      // The manifest is hand-written (public/manifest.json). Tell the
      // plugin not to overwrite it.
      manifest: false,

      workbox: {
        // Precache the build output (CSS/JS chunks, fonts, etc.)
        globPatterns: ['**/*.{js,css,html,svg,woff2}'],

        // Anything that talks to Cloud Run is **never** cached — we want
        // live data. Same for Firestore websockets and Open-Meteo.
        runtimeCaching: [
          {
            urlPattern: ({ url }) =>
              url.host.endsWith('a.run.app') ||
              url.host.endsWith('googleapis.com') ||
              url.host === 'api.open-meteo.com',
            handler: 'NetworkOnly',
          },
        ],

        // SPA navigation fallback — every unknown route inside the app
        // shell loads /index.html which then routes client-side.
        navigateFallback: '/index.html',
        navigateFallbackDenylist: [
          // Don't fall back for API requests to other origins.
          /^\/(api|chat|policy|optimize|jobs|override|learning|health)/,
        ],
      },
    }),
  ],
  server: {
    port: 5173,
    host: '127.0.0.1',
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          // Lazy-load Recharts so /advanced doesn't bloat the Simple-page
          // first paint. Each entry creates a separate chunk file.
          recharts: ['recharts'],
          firebase: ['firebase/app', 'firebase/auth', 'firebase/firestore'],
        },
      },
    },
  },
});
