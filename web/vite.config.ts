import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // Fail rather than drift. Without this Vite silently takes the next free
    // port when 5173 is busy, and the app keeps working -- /api is proxied
    // below, so requests stay same-origin and CORS never fires. What breaks
    // is anything that names the origin: services/api/main.py allows exactly
    // http://localhost:5173 and http://127.0.0.1:5173, so a page served from
    // 5174 loses any call that goes direct to the API instead of through the
    // proxy. That allowlist is deliberately exact and should not be widened
    // to cover a port we did not mean to be on. An error at startup is the
    // cheaper failure.
    strictPort: true,
    // The API allows this origin by CORS, so calls could go direct. The proxy
    // exists so the client can use same-origin relative paths and never needs
    // an API base URL baked into a build.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
