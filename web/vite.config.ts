import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API URL is configurable via VITE_API_BASE. We do NOT proxy through Vite
// because its http-proxy chokes on SSE streams from FastAPI — direct CORS
// fetches to the backend on :8000 are reliable and simpler in dev.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  optimizeDeps: { include: ["pdfjs-dist"] },
  // minifyIdentifiers stays OFF: esbuild otherwise hands out `of` as a
  // 2-char variable name inside elkjs's giant scopes, and vite's bundled
  // es-module-lexer then reads `of/(...)` as the keyword + a regex literal,
  // derails, and kills the build with "Parse error @:1:1". Which variable
  // lands on `of` depends on global name frequencies, so ANY source edit can
  // flip the build red. Whitespace + syntax minify stay on; gzip absorbs
  // most of the size difference.
  esbuild: { minifyIdentifiers: false },
});
