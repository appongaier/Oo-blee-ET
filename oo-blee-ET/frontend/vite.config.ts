import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the UI calls /api and /health on its own origin; Vite forwards them to the FastAPI server.
export default defineConfig({
  plugins: [react()],
  build: { chunkSizeWarningLimit: 900 },   // Cytoscape alone is about 450 kB
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
  // @ts-expect-error vitest config field
  test: { environment: "jsdom", include: ["src/**/*.test.ts", "src/**/*.test.tsx"] },
});
