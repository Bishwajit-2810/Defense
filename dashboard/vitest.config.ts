import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/setupTests.ts',
    css: true,
    // Unit tests live beside the code they test. `tests/` holds Playwright
    // specs, and vitest was picking those up and failing them with "Playwright
    // Test did not expect test() to be called here" — two permanent red files
    // that made `npm test` useless as a signal.
    include: ['src/**/*.{test,spec}.{js,jsx,ts,tsx}'],
    exclude: ['tests/**', 'node_modules/**', 'dist/**'],
  },
  resolve: {
    alias: {
      // `import.meta.dirname` rather than `__dirname`: Vite 8 warns that
      // `__dirname` is unsupported by the `configLoader: 'native'` it plans to
      // default to, and that warning printed on every `npm test` run.
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
});
