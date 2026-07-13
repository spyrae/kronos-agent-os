import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // Split vendor code into stable, long-cached chunks. Matching by
        // node_modules path (not package name) is what catches subpath imports
        // like `react-dom/client` — the object form misses those and leaks the
        // ~180 KB react-dom bulk into the app entry, forcing a re-download on
        // every deploy. React lands in `react-vendor` (eager, cached across
        // deploys); CodeMirror lands in `codemirror`, which stays async because
        // only the lazy Persona/Skills routes import it. Everything else splits
        // per route via the lazy() imports in App.tsx.
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (/[\\/]node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler)[\\/]/.test(id)) {
            return 'react-vendor'
          }
          if (/[\\/]node_modules[\\/](@codemirror|@lezer|@uiw|codemirror)[\\/]/.test(id)) {
            return 'codemirror'
          }
          return undefined
        },
      },
    },
  },
})
