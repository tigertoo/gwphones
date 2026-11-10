import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served at the root of the portal's own site (portal.<domain>/), with the
// API under /api/ on the same site. For local work, `npm run dev` passes
// /api to the portal service on 127.0.0.1:8020.
export default defineConfig({
  plugins: [react()],
  base: "/",
  server: { proxy: { "/api": "http://127.0.0.1:8020" } },
});
