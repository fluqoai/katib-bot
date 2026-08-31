import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite proxies /api → the FastAPI server so the dashboard can be served
// alongside the chat page in dev. In prod, the dashboard is built into
// api/static/admin/ and served by FastAPI directly.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
