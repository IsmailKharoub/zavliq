import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/v1": "http://127.0.0.1:3001",
      "/health": "http://127.0.0.1:3001",
      "/_matrix": "http://127.0.0.1:8080",
      "/_synapse/client/zavliq": "http://127.0.0.1:8080",
      "/.well-known/zavliq": "http://127.0.0.1:3001",
    },
  },
  build: { chunkSizeWarningLimit: 1800 },
});
