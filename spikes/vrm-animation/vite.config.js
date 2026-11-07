import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const root = dirname(fileURLToPath(import.meta.url));

// 直接把 frontend 的 vrm 资产目录当 publicDir：
// 模型走 /Xxx.vrm，动画走 /animations/...，无需复制资产
export default defineConfig({
  root,
  publicDir: resolve(root, "../../frontend/public/assets/vrm"),
  server: { port: 5174, strictPort: true },
});
