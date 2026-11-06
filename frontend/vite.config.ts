import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

// MPA 多入口：Day 3 仅建 pad 入口；console/family 属后续批次（A4 卡：目录占位不建）。
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
    },
  },
  build: {
    rollupOptions: {
      input: {
        pad: resolve(__dirname, "index.html"),
      },
    },
  },
});
