import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

// dev server 下把 /console[/] /family[/] 重写到对应 HTML 文件
function mpaRewritePlugin(): Plugin {
  return {
    name: "mpa-rewrite",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        if (!req.url) return next();
        if (req.url === "/pad" || req.url === "/pad/") {
          req.url = "/index.html";
        } else if (req.url === "/console" || req.url === "/console/") {
          req.url = "/console.html";
        } else if (req.url === "/family" || req.url === "/family/") {
          req.url = "/family.html";
        }
        next();
      });
    },
  };
}

export default defineConfig({
  appType: "mpa",
  plugins: [mpaRewritePlugin(), react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      "/static": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: {
    rollupOptions: {
      input: {
        pad: resolve(__dirname, "index.html"),
        console: resolve(__dirname, "console.html"),
        family: resolve(__dirname, "family.html"),
      },
    },
  },
});
