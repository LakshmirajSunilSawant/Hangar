import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built assets are served by the control plane itself (see hangar/api.py),
// so this builds into a directory FastAPI mounts rather than being deployed
// separately. One deployable is the whole point on a 12 GB box.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // In development the dashboard runs on its own port, so API calls are
    // proxied to the control plane to keep them same-origin.
    proxy: {
      "/apps": "http://127.0.0.1:8080",
      "/healthz": "http://127.0.0.1:8080",
    },
  },
});
