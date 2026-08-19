import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // start.bat opens the browser once after the frontend server starts.
    open: false,
    proxy: {
      // Main FastAPI backend (port 8000). The old /coco -> :8200 proxy to
      // the standalone Ask Coco prototype (ASK COCO/server.py) was removed
      // deliberately: that server has no access-control awareness at all,
      // and nothing in the app is meant to call it anymore now that
      // CocoChatView.tsx/api.ts both go through the authenticated main
      // backend. See ASK COCO/README.md — reference-only, not deployed.
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})

