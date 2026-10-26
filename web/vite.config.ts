import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API serves this build from /ui/. Hashes stay in the filename; nothing is
// inlined, so the page policy can keep script-src, style-src and font-src on
// 'self' with no unsafe-inline and no unsafe-eval.
export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  build: {
    outDir: "../src/flakepilot/interface/http/webdist",
    emptyOutDir: true,
    assetsDir: "assets",
    modulePreload: false,
    cssCodeSplit: false,
    sourcemap: false,
    assetsInlineLimit: 0,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
