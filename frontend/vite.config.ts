import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      output: {
        // Stable vendor chunk: UI edits don't invalidate the React/router cache.
        // The function form is mandatory for Vite 8 / Rolldown (the object form fails).
        // Exact path /node_modules/<pkg>/ — so as not to pull in react-named packages (vidstack,
        // lucide).
        manualChunks(id) {
          if (id.includes('/node_modules/react-router-dom/') || id.includes('/node_modules/react-router/')) return 'router'
          if (id.includes('/node_modules/react-dom/') || id.includes('/node_modules/react/') || id.includes('/node_modules/scheduler/')) return 'react-vendor'
          // Vite splits lucide icons into dozens of tiny chunks — on HTTP/1.1 that's dozens of
          // extra
          // requests. Gather all used icons into a single chunk.
          if (id.includes('/node_modules/lucide-react/')) return 'icons'
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/media': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
