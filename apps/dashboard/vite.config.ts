import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// HESM dashboard — Vite + React + TypeScript.
// Built artifacts go to dist/ and are deployed to Netlify.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: '127.0.0.1',
  },
});
