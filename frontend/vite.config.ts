import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

// MPA 多入口：pad（长者端，React + Tailwind）/ console（照护台，MUI）/ family（家属端，MUI）。
// 分层硬约束 4：pad 专属依赖（如 three-vrm）不得进其他入口构建产物。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      // 后端 TTS WAV 落盘目录（app/api/audio.py 存 <cwd>/static/mock/ 并由 FastAPI 挂载 /static）；
      // dev 下 audio 元素在 5173 源内请求，必须转发到 8000，否则拿到 Vite SPA 兜底 HTML 无声
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
