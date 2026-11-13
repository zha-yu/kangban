# syntax=docker/dockerfile:1
# =============================================================================
# 康伴（SilverMate）前端镜像 · Day 10 C1（dev-C）
# 多阶段：stage1 pnpm 安装依赖 + vite build（容器内全新安装）→ stage2 nginx 托管
#
# 构建命令（Y3 契约口径）：
#   docker build -f frontend/frontend.Dockerfile -t silvermate-frontend frontend/
#   说明：构建上下文 = frontend/ 目录。根 .dockerignore 排除了 frontend/
#   （该文件归 dev-A，本节点不改），故根目录上下文不可用；此差异已登记 C1 报告。
#
# 包管理器口径：frontend/ 唯一锁文件为 pnpm-lock.yaml（lockfileVersion 9.0，
#   由宿主 pnpm 11.15.1 生成），无 package-lock.json，`npm ci` 无法执行。
#   容器内以固定版本 pnpm@11.15.1 --frozen-lockfile 安装；任务书 Y3
#   「npm ci && npm run build」的差异已如实登记 C1 报告，不自行改契约文档。
#   `npm run build` 本身（tsc --noEmit && vite build）不受包管理器影响，照常执行。
#
# 红线：容器内全新安装依赖；严禁 COPY 本机 node_modules / dist / .env / 任何密钥
#   （本 Dockerfile 只做清单与源码的选择性 COPY，无 .dockerignore 依赖）。
# =============================================================================

# -----------------------------------------------------------------------------
# stage 1 · builder：依赖安装 + 三入口 MPA 构建 + 产物分目录重排
# -----------------------------------------------------------------------------
# node:22 LTS（Jod）：宿主锁文件由 pnpm 11.15.1 生成，pnpm 11 依赖 Node 22
# 内置模块（node:20 下报 ERR_UNKNOWN_BUILTIN_MODULE），故 builder 用 22；
# vite 7 要求 node ^20.19 || >=22.12，同样满足。
FROM node:22-bookworm-slim AS builder
WORKDIR /app

# 代理仅构建期生效：本机 HTTP_PROXY=127.0.0.1:52070 容器内不可达，
# 构建时注入 host.docker.internal 变体（--build-arg HTTP_PROXY=...）。
# 仅存在于 builder 阶段 ENV，不进最终镜像，不落任何密钥进镜像层。
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY="localhost,127.0.0.1,backend"
ENV HTTP_PROXY="${HTTP_PROXY}" \
    HTTPS_PROXY="${HTTPS_PROXY}" \
    NO_PROXY="${NO_PROXY}" \
    http_proxy="${HTTP_PROXY}" \
    https_proxy="${HTTPS_PROXY}" \
    no_proxy="${NO_PROXY}"

# 1) 只拷清单文件 → 先装依赖（层缓存友好：源码改动不触发重装）
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN npm install -g --no-fund --no-audit pnpm@11.15.1 \
 && pnpm install --frozen-lockfile

# 2) 拷源码构建（package.json build 脚本：tsc --noEmit && vite build）
COPY tsconfig.json vite.config.ts index.html console.html family.html ./
COPY src ./src
COPY public ./public
RUN pnpm run build

# 3) 产物分目录重排（vite.config.ts 属 dev 外部冻结文件，不改动其配置，构建后重排）
#    目标布局：
#      /app/dist/pad/index.html        + /app/dist/pad/assets/     （pad 独占 chunk）
#      /app/dist/console/index.html    + /app/dist/console/assets/ （console 独占 chunk）
#      /app/dist/family/index.html     + /app/dist/family/assets/  （family 独占 chunk）
#      /app/dist/assets/**             仅剩 public 运行时数据（vrm/avatars 等，
#                                        源码以绝对路径 /assets/** 引用，保持根路径不变）
#    多入口共享 chunk（如 client-*.js）复制进每个入口目录，各入口自包含；
#    根 assets 中的构建 chunk 全部移除，three-vrm 相关 chunk 只在 pad 目录可达。
#    红线判据由此达成：
#      grep -r "three-vrm" dist/console dist/family → 零命中
#      grep -rl  "assets/vrm" dist/pad → 命中（chunk 内含 /assets/vrm/*.vrm 资源引用）
RUN cat > /tmp/arrange.mjs <<'JS'
import { readFileSync, writeFileSync, readdirSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";

const dist = "/app/dist";
const assetsDir = join(dist, "assets");
const entries = { pad: "index.html", console: "console.html", family: "family.html" };
const chunkFiles = new Set(readdirSync(assetsDir));

// chunk 间相对引用（import ... from "./x.js" / import("./x.js") / css url(./x)），
// 从 HTML 直引文件出发求传递闭包
const relRefs = (f) => {
  const s = readFileSync(join(assetsDir, f), "utf8");
  const out = new Set();
  for (const m of s.matchAll(/(?:\bfrom\s*|\bimport\s*\(?\s*|\burl\(\s*)["']?(\.\/[^"'\s)]+)["']?/g)) {
    out.add(m[1]);
  }
  return out;
};
const reachable = (seeds) => {
  const seen = new Set();
  const q = [...seeds];
  while (q.length) {
    const f = q.pop();
    if (seen.has(f) || !chunkFiles.has(f)) continue;
    seen.add(f);
    for (const r of relRefs(f)) q.push(r.replace(/^\.\//, "").split("?")[0]);
  }
  return seen;
};

// 每入口：HTML 直引 → 闭包（含 css 内 url(/assets/…) 对 chunk 的引用）
const plan = {};
for (const [entry, htmlFile] of Object.entries(entries)) {
  const html = readFileSync(join(dist, htmlFile), "utf8");
  const seeds = [...html.matchAll(/\/assets\/([^"'\s?]+)/g)].map((m) => decodeURIComponent(m[1]));
  plan[entry] = { html, files: reachable(new Set(seeds)) };
  for (const f of [...plan[entry].files]) {
    if (!f.endsWith(".css")) continue;
    const s = readFileSync(join(assetsDir, f), "utf8");
    for (const m of s.matchAll(/url\(\s*["']?\/assets\/([^"'\s?)]+)/g)) {
      if (chunkFiles.has(m[1])) plan[entry].files.add(m[1]);
    }
  }
}

// 拷贝 chunk 到各入口目录（css 内对 chunk 的 url 引用同步改写；对 public 数据
// 的绝对引用如 /assets/vrm/... 保持不变）
for (const [entry, { files }] of Object.entries(plan)) {
  mkdirSync(join(dist, entry, "assets"), { recursive: true });
  for (const f of files) {
    let buf = readFileSync(join(assetsDir, f));
    if (f.endsWith(".css")) {
      buf = Buffer.from(buf.toString("utf8").replace(
        /url\(\s*(["']?)\/assets\/([^"'\s?)]+)\1\s*\)/g,
        (full, q, ref) => (files.has(ref) ? `url(${q}/${entry}/assets/${ref}${q})` : full),
      ));
    }
    writeFileSync(join(dist, entry, "assets", f), buf);
  }
}
// HTML 改写为入口相对路径并落到 <entry>/index.html（/favicon.svg 等非 chunk 引用不动）
for (const [entry, { html, files }] of Object.entries(plan)) {
  writeFileSync(join(dist, entry, "index.html"), html.replace(
    /\/assets\/([^"'\s?]+)/g,
    (full, ref) => (files.has(ref) ? `/${entry}/assets/${ref}` : full),
  ));
  rmSync(join(dist, entries[entry]));
}
// 根 assets 只留 public 数据：删尽全部构建 chunk（三入口共享内容已复制进各入口）
for (const { files } of Object.values(plan)) {
  for (const f of files) rmSync(join(assetsDir, f), { force: true });
}
JS
RUN node /tmp/arrange.mjs

# -----------------------------------------------------------------------------
# stage 2 · runner：nginx 托管三入口
# -----------------------------------------------------------------------------
FROM nginx:stable-alpine AS runner

# 默认站点配置：三入口静态 + public 数据 + /api、/ws 反代 backend:8000。
# 上游用变量延迟解析：backend 服务缺席时 nginx 仍可启动（独立冒烟验证用）。
# deploy/nginx.conf 归 dev-A 独占（Y1 静态段草案见 C1 报告），A 可整体挂载覆盖。
RUN rm -f /etc/nginx/conf.d/default.conf
COPY <<'NGINX' /etc/nginx/conf.d/default.conf
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;
    # 相对 Location：容器端口映射 / HTTPS 终止在外层时重定向不带 host:port
    absolute_redirect off;

    # 根路径跳长者端
    location = / { return 302 /pad; }

    # 三入口 MPA（Y3 产物路径：/usr/share/nginx/html/{pad,console,family}）
    location /pad     { try_files $uri $uri/ /pad/index.html; }
    location /console { try_files $uri $uri/ /console/index.html; }
    location /family  { try_files $uri $uri/ /family/index.html; }

    # public 运行时数据（vrm/avatars/kws/vad-assets/media），三入口共用根路径，
    # 实际仅长者端（pad）代码引用
    location /assets/     { try_files $uri =404; }
    location /kws/        { try_files $uri =404; }
    location /vad-assets/ { try_files $uri =404; }
    location /media/      { try_files $uri =404; }

    # API 反代（Y1 草案；上游变量化，backend 缺席时可启动，请求时返回 502）
    location /api/ {
        set $backend_upstream http://backend:8000;
        proxy_pass $backend_upstream;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    # WebSocket 反代（Y1 草案：Upgrade/Connection 头）
    location /ws {
        set $backend_upstream http://backend:8000;
        proxy_pass $backend_upstream;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
NGINX

COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80
