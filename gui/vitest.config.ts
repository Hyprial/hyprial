import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  define: {
    __HYPRIAL_DEV_MODULE_LABELS__: false,
    __HYPRIAL_DEV_PREVIEW__: false,
  },
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx,mjs}'],
    css: false,
  },
});
