import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/chat': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
      '/upload': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
      '/documents': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
    },
  },
})
