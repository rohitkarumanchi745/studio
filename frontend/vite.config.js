import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        // No rewrite: the backend mounts every route under /api, so the
        // browser's /api/* path is the backend's path.
      },
    },
  },
});
