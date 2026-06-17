import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// SkyN3t dashboard build config.
// In dev, proxy /api and /ws to the local FastAPI control plane (default :8000).
const TARGET = process.env.SKYN3T_API || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: TARGET, changeOrigin: true },
      "/ws": { target: TARGET, ws: true, changeOrigin: true },
    },
  },
});
