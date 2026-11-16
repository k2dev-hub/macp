import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The site is prerendered at build time (see scripts/prerender.mjs), so the
// client bundle only needs to hydrate.  No external font or script is loaded:
// the page must be readable with JavaScript disabled, because that is what a
// crawler and a text browser see.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    target: "es2020",
    cssCodeSplit: false,
    assetsInlineLimit: 8192,
  },
  ssr: {
    // Bundle everything into the server entry: antd's dependency tree uses
    // extensionless ESM imports that plain Node cannot resolve, and the
    // prerender script runs outside Vite.
    noExternal: true,
  },
});
