import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: '../../docs/graph/explorer',
    emptyOutDir: true,
  },
  server: {
    // the design system (tokens.css, components.css, marks) lives in docs/design-system
    fs: { allow: ['.', '../../docs/design-system'] },
    proxy: {
      '/api': 'http://127.0.0.1:7890',
    },
  },
})
