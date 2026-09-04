import { resolve } from 'node:path';
import { defineConfig } from 'vite';

// Two static entry points (index.html for the voice client, admin.html for
// the eval-trace dashboard) -- Vite's default build only bundles index.html,
// so admin.html needs to be listed explicitly or a production build
// silently drops it.
export default defineConfig({
  build: {
    rollupOptions: {
      input: {
        main: resolve(import.meta.dirname, 'index.html'),
        admin: resolve(import.meta.dirname, 'admin.html'),
      },
    },
  },
});
