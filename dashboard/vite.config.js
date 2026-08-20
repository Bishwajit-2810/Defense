import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    rolldownOptions: {
      output: {
        // Split the vendor libraries out of the app chunk.
        //
        // The single bundle was 573 kB, which tripped Vite's 500 kB advisory on
        // every build. Measured composition: chart.js + react-chartjs-2 are
        // 179 kB of it, React and React-DOM another 182 kB, lucide's icons 13 kB,
        // and the dashboard's own code 199 kB.
        //
        // This changes nothing about *what* the browser loads — every chunk is
        // still requested on first paint — so it is not a lazy-loading change and
        // carries no runtime risk. What it buys is honest chunking (the fix the
        // advisory actually asks for, rather than raising
        // `chunkSizeWarningLimit` to hide it) and cache granularity: chart.js and
        // React change on dependency bumps, the app chunk changes on every
        // deploy, and they no longer invalidate each other.
        //
        // Deferring the charts until a tab that draws one is opened would be the
        // bigger win, but that needs `React.lazy` + Suspense boundaries around
        // PostModal / Overview / Trace — a runtime behaviour change, deliberately
        // not bundled into a build-config fix.
        manualChunks(id) {
          if (id.includes('node_modules/chart.js') || id.includes('react-chartjs-2')) return 'charts'
          if (id.includes('node_modules/react')) return 'react'
          if (id.includes('node_modules/lucide-react')) return 'icons'
          if (id.includes('node_modules')) return 'vendor'
        },
      },
    },
  },
})
