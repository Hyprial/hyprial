import { defineConfig } from "vite";

export default defineConfig({
  server: {
    port: 3082,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:3081" },
  },
  build: { sourcemap: false },
});
