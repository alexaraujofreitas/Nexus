import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// API target: configurable via VITE_API_BASE_URL for Docker E2E
const apiTarget = process.env.VITE_API_BASE_URL || 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    // Keep CSS in a single file for reliable loading through Cloudflare Tunnel
    cssCodeSplit: false,
  },
  server: {
    host: true,
    port: 5173,
    allowedHosts: ['frontend', 'nexustrader', 'localhost', 'www.kivikdg.com', 'kivikdg.com'],
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
      '/ws': {
        target: apiTarget.replace('http', 'ws'),
        ws: true,
      },
    },
  },
})