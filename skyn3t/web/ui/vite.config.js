import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// SkyN3t dashboard build config.
// In dev, proxy /api and /ws to the local FastAPI control plane (default :6660).
const TARGET = process.env.SKYN3T_API || "http://127.0.0.1:6660";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    // three.js is inherently large; it's isolated into its own chunk and only
    // loaded with the Brain route, so allow it past the default 500 kB warning.
    chunkSizeWarningLimit: 850,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("three") || id.includes("@react-three")) return "three";
          if (id.includes("react-router")) return "router";
          if (id.includes("@tanstack")) return "query";
          if (id.includes("/react/") || id.includes("/react-dom/") || id.includes("/scheduler/"))
            return "react-vendor";
          return undefined;
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      // Preserve the browser-facing Host for the control-plane Origin check.
      "/api": { target: TARGET, changeOrigin: false },
      "/ws": { target: TARGET, ws: true, changeOrigin: false },
    },
  },
});
