import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Default backend port — keep in sync with chimera/monitor/cli.py DEFAULT_PORT.
const BACKEND_PORT = process.env.CHIMERA_MONITOR_PORT
  ? Number(process.env.CHIMERA_MONITOR_PORT)
  : 8740;

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${BACKEND_PORT}`,
        changeOrigin: true,
        // SSE-friendly: keep connections open indefinitely. Phase 2 SSE depends
        // on this — Vite's default 30s idle would otherwise sever streams.
        timeout: 0,
        proxyTimeout: 0,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
