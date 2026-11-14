import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: '../server/static',
    emptyOutDir: true,
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    // Match the local IPv4 backend even when Node resolves localhost to ::1.
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
