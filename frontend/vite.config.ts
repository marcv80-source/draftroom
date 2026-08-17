import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

// Draft night runs with wifi physically off (repo CLAUDE.md): this must build to a fully
// self-contained static bundle with no CDN/remote asset references. There is no dev server
// on draft night -- `npm run build` produces the artifact `DraftNight.bat` actually serves,
// straight into the FastAPI app's static directory.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: resolve(__dirname, "../backend/draftroom/static"),
    emptyOutDir: true,
    assetsDir: "assets",
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8484",
      "/ws": {
        target: "ws://127.0.0.1:8484",
        ws: true,
      },
    },
  },
});
