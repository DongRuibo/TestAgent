import { defineConfig } from "vite";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    emptyOutDir: true,
    lib: {
      entry: fileURLToPath(new URL("./src/generation-ui-bridge.js", import.meta.url)),
      formats: ["es"],
      fileName: () => "generation-ui.js",
      cssFileName: "generation-ui",
    },
    outDir: fileURLToPath(
      new URL("../src/web_agent/static/generation-ui", import.meta.url),
    ),
  },
});
