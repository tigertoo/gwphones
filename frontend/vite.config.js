import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base must match the nginx location this gets served from (/admin/),
// otherwise the built <script>/<link> tags will point at the wrong paths.
export default defineConfig({
  plugins: [react()],
  base: "/admin/",
});
