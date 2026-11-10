# 康伴（SilverMate）· 3D 数字人康养陪伴系统

> 一间长者卧室里的「会说话的屏」：浏览器页面上的 VRM 数字人，听得懂方言语音，会陪着聊天、提醒吃药、每晚问一遍身体状况、放一段戏曲，遇到急症立刻叫人。

---

## 1. 背景与目标

CCRC 康养社区的长者卧室普遍只有一块平板。康伴把这块平板变成一位**有形象、有声音、有记忆的陪伴者**，同时把照护过程中最容易漏掉的三件事（用药确认、健康采集、急症预警）变成可追溯的数据。

系统同时服务三类使用者：

| 端 | 使用者 | 界面口径 |
| --- | --- | --- |
| `pad` 长者端 | 长者本人 | 1920×1080 全屏、大字大按钮、字幕正文 ≥20px、触控区 ≥48×48px |
| `console` 照护台 | 护工 / 医生 / 管理员 | 按角色分流：护工看提醒与工单，医生维护药单与趋势图，管理员管人员与设备 |
| `family` 家属端 | 长者家属 | 375px 移动优先、无横向滚动、正文 ≥16px |

**工程目标**：端到端语音往返 P95 ≤ 1.5s；≥60% 的请求在快决策层闭环；语音识别用方言仍可用；`docker compose up` 一键起全栈。

> ⚠️ **无真实平板硬件**：全部前端都是 HTML 页面，用浏览器全屏模拟平板界面。这是本项目的既定约束，不是临时方案。

---

## 2. 业务边界（11 条非目标，逐条硬性约束）

产品能力的扩张以不越过下面 11 条为界。**任何越过边界的实现都视为缺陷，不是功能。**

| # | 不做 | 边界说明 |
| --- | --- | --- |
| 1 | **不做医疗诊断** | 绝不说「您这是高血压 / 糖尿病」。异常值只说「这个数值偏高，建议问问医生」，同时触发人工告警 |
| 2 | **不替代 SOS 紧急呼叫** | 急症走「提醒护工」通道，不承诺替代 120 或社区应急系统 |
| 3 | **不做高精度 3D 建模 / 动捕** | 用现成 VRM 形象 + 音频能量近似口型，不追求影视级驱动 |
| 4 | **不做 AI 歌曲生成** | 早期方案里的 yue2 已删除，只做点播 |
| 5 | **不依赖商业 Jev API** | System One 用本地开源权重（Laya），不付费调商业意图分类服务 |
| 6 | **不做盗版曲库 / 影库** | 仅使用教师提供的 20–30 条授权样例，不爬取、不抓取、不内置来源不明内容 |
| 7 | **不接支付 / 采购 / 门禁** | 与社区商业系统无集成 |
| 8 | **不存完整病历与原始录音** | 只存结构化摘要 + 脱敏文本；原始音频最长 72 小时，由定时任务自动删除 |
| 9 | **不做移动端原生 App** | 家属端是移动优先的 Web 页面 |
| 10 | **不做多租户 SaaS** | 单社区部署，不做 SaaS 化改造 |
| 11 | **不做完整 diarization** | 只做「当前说话人是否本人」的声纹判定，不做多人分离转写 |

补充（来自 PRD）：不接真实 HIS / 医保 / 医院 / 急救 / 微信；不做生产级声纹认证与视觉识别；长期记忆**仅本人可见**，不做公私共享。

---

## 3. 架构总览

### 3.1 一次对话的链路

```
浏览器（pad / console / family，HTML 模拟平板）
   │  语音帧 ▲ PCM 音频轨
   ▼        │
FastAPI（WebSocket 信令 SDP/ICE + aiortc 媒体）
   │
   ├─► 流式 ASR ──► 文本 + 置信度
   │
   ├─► ① 规则层（safety 一票否决 + ≤10 条固定意图）        零延迟，命中即返回
   │        └─ 未命中 ▼
   ├─► ② System One：Laya 本地 CPU 分类器                    单句 ≤200ms
   │        ├─ confidence ≥ 0.85 → 直接路由到能力
   │        └─ < 0.85 → 仅澄清一次 → 仍模糊则下沉
   │
   └─► ③ System Two：主控 Agent（ReAct + Tool Calling）      开放域兜底
            ├─ LangGraph 子图：用药确认 / 健康采集（带 HITL 与断点恢复）
            ├─ MCP Tool：get_weather / create_work_order / send_family_notification
            └─ 预算 MAX_TOOL_CALLS=8，同 Tool 1 分钟 3 次熔断
   │
   ▼
流式 TTS ──► WebRTC 音频轨回放 + 页面大字幕（字幕必须与语音内容一致）
```

### 3.2 分层与技术栈

| 层 | 内容 | 技术 |
| --- | --- | --- |
| 交互层 | 三端 HTML 页面、3D 形象、字幕、提醒卡片 | React + Vite（MPA）、Tailwind / MUI、three.js + three-vrm |
| 语音层 | WebRTC 全双工、反向提醒外呼、降级上传 | aiortc、coturn、webrtcvad、第三方流式 ASR/TTS |
| 决策层 | 规则层 → System One → System Two | 纯代码规则、Laya（CPU）、DeepSeek / Qwen 兼容 API |
| 编排层 | 主控 Agent、两个业务子图、MCP 工具 | LangGraph + PostgreSQL Checkpointer、MCP |
| 数据层 | 业务表、记忆向量、checkpoint、缓存 | PostgreSQL 15 + pgvector、Redis |
| 运维层 | 容器、观测、评测门禁 | Docker Compose、Langfuse + OpenTelemetry + `/metrics`、pytest |

> 详细选型理由与逐项口径见 `docs/康伴3D数字人_技术设计文档.md` §1.4；工程约束与禁令见 `AGENTS.md`。

---

## 4. 目录结构

```
.
├── app/                 # 后端：core / api / audio / router / system_one / agents / graphs / services / models / mcp_client / scheduler / prompts
├── mcp-servers/         # weather（真实 API + 缓存）、mock-operations（工单/通知落库）
├── frontend/            # index.html（MPA 声明）+ src/{pad,console,family}
├── alembic/versions/    # 分批迁移，主迁移合入后不再改
├── eval/fixtures/       # 固定测试集（普通 50 / 敏感 20 / 边界 20 / 模糊 20 / 注入 ≥10）
├── eval/reports/        # 基线与 bad case 回流报告
├── tests/               # test_gates.py / test_injection.py / eval_gate.py
├── spikes/              # 新依赖最小验证，跑通才合并
└── docs/                # PRD、大纲、技术设计、ER、HARNESS 等基线文档
```

完整目录语义与**分层硬约束**（`services` 是唯一写库层、模块间不得横向 import、three-vrm 不得进 console/family 产物）见 [`AGENTS.md`](AGENTS.md) §2。

---

## 5. 快速开始

### 5.1 前置工具

| 工具 | 用途 | 备注 |
| --- | --- | --- |
| Python 3.12（要求 3.12+） | 后端运行时 | `.python-version` / `requires-python` / ruff / mypy 四处统一锁 3.12 |
| [uv](https://docs.astral.sh/uv/) | 唯一的 Python 包管理器 | 不要混用 pip / conda，本仓库 `.venv` 由 uv 管理 |
| Docker + Docker Compose | 起 PG / Redis / FunASR 等依赖 | 完整 7 服务版（含前后端容器）留到 Day 10 |
| Node.js + pnpm | 前端构建 | 三入口共用一套 Vite 配置 |
| mkcert（可选） | 本地 HTTPS 证书 | 麦克风在非 localhost 下必须 HTTPS |

> **Windows 不需要装 Microsoft C++ Build Tools**。VAD 依赖用的是 `webrtcvad-wheels`（上游 `webrtcvad` 的同源 fork，import 名与 API 完全不变），自带 cp310–cp314 预编译 wheel，`uv sync` 直接取二进制。若改回上游 `webrtcvad`，Windows 上会因缺少 C++ 编译器而 `uv sync` 失败。验证脚本：`uv run python spikes/vad/spike_webrtcvad.py`。

### 5.2 前后端启动命令

当前 `docker-compose.yml` 只编排 **postgres / redis / funasr** 三个依赖服务，仓库还没有 `Dockerfile`、nginx 与 backend/frontend 容器。本地开发的口径是「**基础组件走 Docker，前后端各起一个终端**」；完整 7 服务一键起留到 Day 10。

#### 终端 A · 后端（FastAPI，:8000）

```bash
# 首次：装依赖 + 配环境变量（.env 已被 .gitignore 忽略，绝不入库）
uv sync
cp .env.example .env          # 已有 .env 可跳过；至少填 LLM_API_KEY、ASR_*/TTS_*、WEATHER_API_KEY

# 起依赖并建表（页面/接口联调只需 postgres + redis；要用本地 ASR 基线再追加 funasr）
docker compose up -d postgres redis
uv run alembic upgrade head

# 每次启动
uv run uvicorn app.main:app --reload --port 8000
```

```bash
curl -s localhost:8000/health   # {"status":"ok", ...}，响应头带 X-Trace-Id
```

接口文档在 `http://localhost:8000/docs`。`/health` 刻意不探库，没有 `.env`、没起 PG/Redis 也能 200；联调按任务卡口径去掉 `--reload` 即为非热重载模式。

#### 终端 B · 前端（Vite，:5173）

```bash
pnpm --dir frontend install     # 首次，或 package.json / pnpm-lock.yaml 变更后
pnpm --dir frontend dev         # 开发服务器，strictPort: true，端口被占会直接报错退出
pnpm --dir frontend build       # tsc --noEmit + MPA 构建，产物 frontend/dist/
pnpm --dir frontend preview     # 本地预览构建产物
```

| 项 | 说明 |
| --- | --- |
| 页面入口 | 当前 MPA 只有 `pad`：`http://localhost:5173/`；`console` / `family` 入口随 Day 4 落地（`/console.html`、`/family.html`），以 `frontend/vite.config.ts` 的 `rollupOptions.input` 为准 |
| 接口代理 | `vite.config.ts` 把 `/api` → `http://localhost:8000`、`/ws` → `ws://localhost:8000`；**先起后端再起前端**，页面里不用配 API 地址，也不触发 CORS |
| HTTPS | 本机 `localhost:5173` 可直接使用麦克风；跨机联调需 mkcert 本地证书，否则 `getUserMedia` 不可用 |
| 构建期变量 | `VITE_*` 只在 `pnpm --dir frontend build` 时注入，dev 模式走上面的代理，不必改 `.env` |

**没有 `uv` 的机器**（WSL 等环境：`.venv` 与系统 `python` 都没有 `pip`）：上面的 `uv sync` 起不动，改用容器跑同一批命令。

```bash
docker run -d --name silvermate-verify -v "$PWD":/w -w /w python:3.12-slim sleep infinity
docker exec silvermate-verify pip install fastapi "uvicorn[standard]" pydantic-settings \
  sqlalchemy alembic "psycopg[binary]" pytest pytest-asyncio httpx ruff
docker exec -w /w silvermate-verify sh -c 'PYTHONPATH=/w python -m pytest -q -m "not slow"'
docker exec -w /w silvermate-verify sh -c 'PYTHONPATH=/w exec uvicorn app.main:app --port 8000'
```

需要连 PostgreSQL 跑迁移的完整版本见 [`AGENTS.md` §3.1](AGENTS.md)。

> **当前进度（2026-11-09）**：Day 1–Day 5 已交付；**Day 6「长者侧 Agent 完整闭环」在推进中**——A 线（历史账本 / 工具执行 / 用药子图 / 工单 / 天气 MCP 接入）已收口，验收 **931 passed、覆盖率 81.51%**，取证见 §7.4；B 线（状态中心与上下文记忆）与 C 线（Agent、统一出口、安全层）的代码已在 `develop`，联调与门禁随全队节奏收口。
> 已就绪：`docker-compose.yml`（基础依赖版）、`frontend/`（pad / console / family 三端）、`app/`、`alembic/`（至 `0011`）、`tests/`、`mcp-servers/weather/`。
> 尚未就绪：`Dockerfile`、nginx 托管、`eval/`（评测基线，随 Day 7 之后落地）——在此之前「全栈一键起」不可用，按上面两个终端分别启动。

---

### 5.3 本机已知环境坑（2026-11-05 实测）

| 现象 | 根因 | 规避 |
| --- | --- | --- |
| 仓库根冒出 `pytest-of-Administrator/` | 本机 `TEMP` 是 8.3 短名（`ADMINI~1`），`tempfile.gettempdir()` 校验失败后回退到 CWD，pytest 的 tmp 工厂于是落在仓库根 | 已加 `.gitignore` §12；要根治可给 pytest 传 `--basetemp` 指向长路径目录 |
| 仓库外写不动（`%LOCALAPPDATA%\Temp`、`~/.cache` 报 `WinError 5`） | 工具沙箱只放开工作区 | 临时产物一律写仓库内已被忽略的目录（如 `spikes/**/out/`） |
| `uv run alembic` 报 `UnicodeDecodeError: 'gbk' codec can't decode byte 0x89` | configparser 用平台 locale 读 `alembic.ini` | **`alembic.ini` 必须 ASCII-only**，注释里不许出现中文/全角 |
| 容器内 edge-tts 转码报 `TTSUnavailable("ffmpeg not found")` | `python:3.12-slim` 不带 ffmpeg | 设计内行为：生产走云端 TTS，edge 只是宿主机兜底；容器跑测试时相关用例自动 skip |
| 容器在两次工具调用之间被回收（`docker ps -a` 显示 `Exited (0)`） | 环境在调用结束后回收容器 | 「起库 → 另开一次调用跑测试」必然失败；改成**库与测试同容器、一次调用跑完** |
| `docker run ... bash -s <<'SH'` 返回空输出 | heredoc 经 `wsl` 工具传递被吞 | 用 `wsl-write` 落脚本到 `/tmp/*.sh`，再 `-v /tmp/x.sh:/v.sh:ro` 挂进容器执行 |

## 6. 配置说明

所有密钥只走环境变量，由 Pydantic Settings 从 `.env` 加载；`.env.example` 只有占位符。**三份基线文档没有枚举任何环境变量名与端口**，下表是本项目自定义并回填文档的约定。

| 分组 | 变量 | 说明 |
| --- | --- | --- |
| 应用 | `APP_ENV` `DEBUG` `API_HOST` `API_PORT` `PUBLIC_BASE_URL` `FRONTEND_ORIGIN` | 运行环境、监听地址、对外域名 |
| 鉴权 | `JWT_SECRET` `JWT_ALGORITHM` `ACCESS_TOKEN_EXPIRE_MINUTES` `REFRESH_TOKEN_EXPIRE_DAYS` `ELDER_SESSION_EXPIRE_HOURS` | 人（2h/7d）、设备（24h）、工牌三套体系 |
| 数据库 | `DATABASE_URL` `LANGGRAPH_CHECKPOINT_DATABASE_URL` `LANGGRAPH_CHECKPOINT_SCHEMA` | checkpoint 必须落独立 schema `langgraph` 与独立连接池 |
| 缓存 | `REDIS_URL` | 短期会话、限流、重复调用检测 |
| System Two | `LLM_BASE_URL` `LLM_API_KEY` `LLM_MODEL` `LLM_TIMEOUT_SECONDS` | DeepSeek / Qwen 兼容端点，经统一适配器 |
| System One | `SYSTEM_ONE_ENABLED` `SYSTEM_ONE_PROVIDER` `SYSTEM_ONE_BASE_URL` `SYSTEM_ONE_API_KEY` `SYSTEM_ONE_TIMEOUT_MS` `SYSTEM_ONE_CONFIDENCE_THRESHOLD` `SYSTEM_ONE_MODEL_ID` `SYSTEM_ONE_MODEL_PATH` `SYSTEM_ONE_MODEL_VERSION` `SYSTEM_ONE_DEVICE` `SYSTEM_ONE_MAX_CONCURRENCY` | 本地 Laya multilingual 权重 + sidecar，CPU 推理；阈值默认 0.85、超时 200ms。旧的 `LAYA_*` 仍可读但会打一次弃用告警（新代码禁读） |
| 语音 | `ASR_*` `TTS_*` | 第三方流式服务，教师统一发 key |
| 实时通话 | `TURN_URL` `TURN_USERNAME` `TURN_CREDENTIAL` `STUN_URL` `WEBRTC_ICE_TIMEOUT_SECONDS` `WEBRTC_VAD_AGGRESSIVENESS` | ICE 超时触发降级；服务端 webrtcvad 兜底判定强度 0~3，默认 3（adhoc-20261106） |
| 天气 | `WEATHER_API_PROVIDER` `WEATHER_API_KEY` `WEATHER_API_BASE_URL` `WEATHER_CACHE_TTL_SECONDS` | 全系统唯一真实外部接口 |
| 通知 | `NOTIFY_CHANNEL` `WEBPUSH_VAPID_PUBLIC_KEY` `WEBPUSH_VAPID_PRIVATE_KEY` | 仅 `inapp` / `webpush`，**无微信、无邮件** |
| MCP | `MCP_WEATHER_SERVER_URL` `MCP_OPERATIONS_SERVER_URL` `MCP_CALL_TIMEOUT_SECONDS` | 工具发现与超时 |
| 观测 | `LANGFUSE_*` `OTEL_*` `METRICS_ENABLED` | Trace 上传前脱敏 |
| 数据保留 | `AUDIO_RETENTION_HOURS` `SCHEDULER_TIMEZONE` | 原始音频默认 72h 自动清理 |
| 前端构建期 | `VITE_API_BASE_URL` `VITE_PAD_WS_URL` `VITE_ENABLE_3D` `VITE_ENABLE_WEBRTC` | 以 `VITE_` 前缀暴露给浏览器 |
| 3D 资产 | `DEFAULT_AVATAR_URL` `DEFAULT_FALLBACK_AVATAR_URL` `AVATAR_ROTATION_Y_OFFSET` `AVATAR_CACHE_VERSION_SALT` | 形象取值链**第三层**兜底（persona → Settings → 前端内置），见 §6.1 |

**本地 ASR 基线（FunASR）**：**不新增地址类变量**，复用现有 `ASR_*` 字段——`ASR_PROVIDER=funasr`、`ASR_BASE_URL=wss://localhost:7002`、`ASR_INSECURE=true`（宿主端口见下表 `funasr` 行，容器内 FunASR runtime 监听 10095）。

⚠️ **这个服务默认开 TLS，且用的是镜像内置自签证书**（容器启动日志固定打印 `SSL is opened!` + `certfile ../../../ssl_key/server.crt`）。所以：
- 用 `ws://` 连会被直接掐断，报 `did not receive a valid HTTP response`——**必须用 `wss://`**；
- 自签证书需要 `ASR_INSECURE=true` 才能握手成功；生产环境应换成真证书并把该开关置回 `false`；
- 部分环境下 Docker 的端口发布（宿主 `7002` → 容器 `10095`）从宿主侧不可达，此时让调用方加入同一 compose 网络、直接用容器名访问 `wss://silvermate-funasr:10095`（实战验证用的就是这条路）。

云 API 仍是主实现，FunASR 只作本地基线与降级备份（见 `docs/plans/task-a1-ASR与TTS适配层.md`）。

### 6.1 3D 形象资产与取值链

平板渲染数字人所需的四个值由 `GET /api/pad/avatar-config` 给出（契约 C7，设备 JWT 鉴权）：

```json
{"avatar_url": "https://localhost/avatars/vrm1_sample.vrm",
 "fallback_image_url": null, "rotation_y_offset": 0.0, "version": 1686351657}
```

`avatar_url` 的取值顺序是**三层**，前一层为空才落到下一层：

| 层 | 来源 | 改它的方式 |
| --- | --- | --- |
| 1 | `personas.avatar_url`（库里存**相对路径**，响应时拼 `PUBLIC_BASE_URL`） | 改 persona 行即可换形象，**零代码、零重启**；`version` 指会随之变化，前端缓存自动失效 |
| 2 | `DEFAULT_AVATAR_URL` | 改 `.env` 后重启后端 |
| 3 | 前端内置兜底（`VITE_ENABLE_3D=false` 或前两层皆空时的静态头像） | 改前端构建期变量 |

`version` 是四键内容的 crc32 指纹（不是自增版本号），因此它只承担「缓存失效」职责，**不代表形象可切换的档位**（FR-3D-03「更多动作与形象」是 P2，不排期）。

**静态托管**：`.vrm` 由 nginx 直出，不经后端转发（PRD §2.1）。片段在 `deploy/nginx.conf` 的 `location /avatars/`，要点两条：

- `.vrm` 必须回 `Content-Type: model/gltf-binary`，否则 three-vrm 拒绝解析（配置里用 `types { model/gltf-binary vrm; }` 显式声明，不依赖系统 mime.types）；
- 目录映射 `deploy/assets/avatars/` ↔ URL `/avatars/`，文件体积上限 **15MB**（超过会挤掉 FR-3D-02 的 5s 降级窗口）。

**资产不入库**：`*.vrm` 与 `*.glb` 已被 `.gitignore` 忽略（`.vrm` 单个 10MB 量级）。新克隆仓库后按 `deploy/assets/README.md` 重新下载：

```bash
curl -L -o deploy/assets/avatars/vrm1_sample.vrm \
  https://pixiv.github.io/three-vrm/packages/three-vrm/examples/models/VRM1_Constraint_Twist_Sample.vrm
```

一次性验证 nginx 片段（无需常驻服务，Docker 在 WSL 里即可）：

```bash
wsl -e sh -lc "docker run --rm -v /mnt/e/project_month/silvermate/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro nginx:alpine nginx -t"
```



### 6.2 服务与端口

| 服务 | 端口 | 说明 |
| --- | --- | --- |
| postgres + pgvector | 5432 | 业务表 + 独立 schema `langgraph` |
| redis | 6379 | 缓存与限流 |
| backend（FastAPI + aiortc） | 8000 | `/health` 健康检查；WebSocket 信令 |
| frontend（nginx） | 80 / 443 | 静态资源 + HTTPS 入口 + 媒体直连 |
| system-one（sidecar） | 7001 | 本地 Laya multilingual 推理；`GET /health`、`POST /v1/decide`。端口沿用 Day 6 的 laya 口径，未变更 |
| funasr | 7002 | 本地 ASR 基线（FunASR runtime） |
| weather-mcp | 7101 | `get_weather` |
| mock-operations-mcp | 7102 | 工单 / 家属通知 |
| coturn | 3478 / 5349 | TURN/STUN，跨网段才需要 |
| vite dev server | 5173 | 仅本地开发 |

### 6.3 账号维护（管理员专属，2026-11-06）

账号不由使用者自助注册：员工与家属账号一律由 `admin` 创建，家属建号后还要再绑定到长者（两步）。

| 能力 | 端点 | 说明 |
| --- | --- | --- |
| 建员工 | `POST /api/staff` | `{name, role, phone, password, elder_ids?}`；`role ∈ admin / care_worker / doctor`；`elder_ids` 同时写负责范围（doctor→`doctor`、care_worker→`primary_care`，给 admin 分配一律 403） |
| 改员工 | `PATCH /api/staff/{staff_id}` | `{role?, status?}`，任一变更 `token_version+1`，旧 Token 立即失效（AC-24） |
| 建家属 | `POST /api/guardians` | `{name, phone, password}` → 返回 `guardian_id` |
| 绑家属 | `POST /api/elders/{elder_id}/guardians` | `{guardian_id, relation}`；重复绑定 409；同一长者最多 2 位在册监护人（服务层校验） |

错误码：`401 TOKEN_INVALID` / `403 ROLE_FORBIDDEN` / `404` / `409 CONFLICT`（手机号跨员工与家属两表查重）/ `422`。响应只回 `phone_masked`，不回传完整手机号与口令哈希。

**空库先建第一个 admin**——没有任何 admin 时上面四个接口都进不去，用 CLI 引导：

```bash
uv run python scripts/create_account.py --role admin --name 王管理 \
  --phone 13700000000 --password 'Admin@2026'
```

CLI 幂等（手机号已存在则 `kept`）、只存 bcrypt 哈希、支持 `--dry-run` 与 `--status`。契约见 `docs/接口契约_身份与权限.md` E12/E13/E15。

---

## 7. 质量门与验收

| 项 | 标准 |
| --- | --- |
| 核心 Tool 与权限场景 | 用例覆盖 **100%** |
| 接口测试覆盖率 | **≥ 70%**（当前 81.51%；统计口径 `--cov=app`，`fail_under=70` 已在 `pyproject.toml` 设死） |
| 测试执行口径 | `pytest -q -m "not slow"`。**不带 `-m` 会因外网 edge-tts 用例失败**，属环境依赖而非代码问题，见 §7.4 |
| 三道隔离闸门 | 会话隔离 / 记忆隔离（强制 `WHERE elder_id`）/ 工具授权，**100% 覆盖** |
| 提示注入测试集 | ≥10 条，**100% 拦截**；日志脱敏 grep 零命中 |
| 评测门禁 | `eval_gate.py` 作为 pre-push 钩子，固定集不达标阻断提交，bad case 回流 `eval/fixtures` |
| 端到端延迟 | P95 ≤ 1.5s；规则层路由 ≤200ms |
| 一键部署 | 全部服务健康检查通过 + 长者端 HTTPS 可访问 + 可回滚 |

硬性数值（`MAX_TOOL_CALLS=8`、0.85 阈值、用药四次触达、19:00–20:00 采集窗口、72h 音频保留等）在 [`AGENTS.md`](AGENTS.md) §7 有完整清单，**改动需走评审**。

### 7.1 Day 2 验收记录（2026-11-05）

三层评审顺序：AI 自审 diff → `pytest` 全绿 → 人工抽查 `auth` / JWT 与 `0002` 迁移（AGENTS §5）。

| DoD 项 | 结论 | 取证 |
| --- | --- | --- |
| `alembic upgrade head` / `downgrade -1` 双向可跑 | ✅ | 升降级闭环后 `alembic current` = `0003_fix_day1_ck_names (head)` |
| 错密码 401 / 伪造 token 401 / 停用后旧 token 401 | ✅ | `tests/test_auth_api.py`（含 `test_login_wrong_password_returns_401`、`test_fake_token_returns_401`、`test_disabled_account_old_token_401`） |
| `/auth/me` 解角色、设备 token 解 `device_id` | ✅ | `test_current_identity_staff` / `test_device_session_success`；Iter 1 实测返回 `{kind: staff, role: admin, ...}` |
| 非管理员 `POST /elders` 403、护工查非负责长者 403 | ✅ | `test_t02`、`test_t04`/`test_t05` + `tests/test_auth_attacks.py` |
| 3 条攻击用例入库（伪造 / 越权建档 / 设备重放） | ✅ | `tests/test_auth_attacks.py`，全部走真实依赖与真实库 |
| `pytest` 全绿、`ruff check app tests` 干净 | ✅ | 270 passed；`All checks passed!` |
| Iter 1 演示一次走通 | ✅ | 登录 200 → 解角色 200 → 建档 201 → 读 200 → 改 200 → 列表 200 → 归档 200；日志 18 处携带同一 `trace_id` |

遗留（不属本卡，已发回对应主责人）：

- `ruff format --check app tests` 在 `app/api/auth.py`、`app/services/auth_service.py`、`tests/test_auth_api.py` 三处红，属 task-5 合入代码；
- `mypy app` 在 `app/api/elders.py:86`（`Awaitable` 与父类 `Coroutine` 不兼容）和 `app/services/device_service.py`（可空列传给非空字段）两处红，属 task-6 合入代码；
- 人工逐行审查 `app/main.py` 的 router 装配与 `tests/test_auth_attacks.py` 后，task-7 方可置为 ✅。

### 7.2 Day 3 验收记录（2026-11-05）

A5 角色，Blocks 1–4 全部落盘并通过质量门；核心交付为**按住说话降级链路**（WebRTC 主链路不可用时的唯一回退）+ webrtcvad 服务端兜底 VAD + Mock ASR/TTS 依赖注入工厂 + 攻击面+降级话术三场景覆盖。

| DoD 项 | 结论 | 取证 |
| --- | --- | --- |
| C3 契约 TypedDict 冻结 | ✅ | `app/api/audio.py` 顶部 `UtteranceReply` / `UtteranceResponse`；`docs/team_tasks_day03/contracts.md` C3 标记已冻结 |
| webrtcvad 兜底包装层（帧常量 + 前置校验 + VadSession） | ✅ | `app/audio/vad.py`；spikes/vad/spike_webrtcvad.py 已验证底层 webrtcvad 原生抛 `_webrtcvad.Error`（非 ValueError），包装层靠 `valid_rate_and_frame_length` 前置拦截 |
| VadSession 状态机（preRoll + 静音/说话双状态机 + 30s 强制切段） | ✅ | `feed_frame` / `flush` / `reset` / `is_speech` 四对外契约 |
| `POST /api/audio/utterance` 完整链路 | ✅ | 鉴权 → WAV 头真实时长校验（不信任客户端 `duration_ms`）→ Mock ASR → Day 3 写死话术 → Mock TTS → `audio_url` |
| Mock ASR/TTS 依赖注入（支持测试 override unavailable 场景） | ✅ | `_get_default_asr` / `_get_default_tts` 两个 FastAPI 依赖函数；端点签名 Depends 参数；`test_audio_security.py` 通过 `app.dependency_overrides[...] = mock_*_factory(unavailable=True)` 控制 |
| 降级话术四常量集中 | ✅ | `DEGRADE_TEMPLATES` dict：`asr_unavailable` / `tts_unavailable` / `llm_timeout` / `tool_breaker`，每条 ≤40 字 |
| Day 3 Mock 链路与后续批次边界清晰 | ✅ | Block 3 注释明确：ASR/TTS/回复分别在 Day 6 接真链路；不引入第二种 VAD 形态；不碰 services/models |
| `ruff check app tests` 干净 | ✅ | `All checks passed!`（清理脏 `__pycache__` 后重跑） |
| `pytest` 全绿 | ✅ | **292 passed, 0 failed, 0 skip**（比 Day 2 的 270 增 22：vad 12 + utterance 5 + security 5） |
| 接口覆盖率 ≥70% | ✅ | **TOTAL 85%**（PRD FR-TC-06 门槛）；audio.py 98%、vad.py 95% |
| 攻击面增量：伪造 token → 401 | ✅ | `test_fake_token_401` |
| 攻击面增量：revoked token_hash 轮换 → 401 | ✅ | `test_revoked_device_token_401`（改 DB `device.token_hash` 模拟解绑） |
| 降级话术场景 1：ASR 不可用 → 503 + 话术 | ✅ | `test_asr_unavailable_503` |
| 降级话术场景 2：TTS 不可用 → 200 + `audio_url=null` + 话术前缀 | ✅ | `test_tts_unavailable_200` |
| 降级话术场景 3：ASR+TTS 双不可用 → ASR 先抛 503 | ✅ | `test_asr_then_tts_both_unavailable_asr_wins` |
| 边界：损坏 WAV 头不 crash | ✅ | `test_corrupt_wav_header_not_crash`（伪造 96 字节 "RIFF\x00..." → `_wav_duration_ms` 内部 `wave.Error` 返回 0 → 正常继续 → 200） |
| 边界：elder_id 从设备绑定注入，客户端传参被忽略（禁区 9） | ✅ | `test_client_elder_id_ignored`（FastAPI 根本没这参数，自动忽略） |

Day 3 调试中发现并修复的 4 个坑（详见会话，均已写进代码注释/测试）：

| # | 现象 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | 全 0 静音帧 webrtcvad aggressiveness=2 随机判为 speech | webrtcvad 对极低能量帧判定不稳定 | 测试改用低能量非零帧（幅值 5 的 1Hz 正弦波） |
| 2 | 连续浊音后静音门限 27 帧不够 | webrtcvad 有内置 hangover（句末保护），前 3 帧静音仍判为 speech | 测试补偿 3 帧；生产代码不修（90ms 延迟可接受） |
| 3 | FastAPI v2 `Form(default=None)` 不能放 `Annotated` 里 | v2 行为变更 | 改 `duration_ms: int \| None = Form(default=None)` |
| 4 | `Depends` 参数不能放在有默认值的 `Form` 参数之后 | Python 语法限制 | 把 `asr`/`tts` 挪到前面 |

---

### 7.3 Day 4 · A3 3D 展示后端验收记录（2026-11-06）

任务卡：`docs/team_tasks/day04/task-03-3D展示后端.md`｜实施计划：`docs/plans/day04/a3-3D展示后端.md`｜执行 DAG：`docs/plans/day04/a3-DAG.md`

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 设备 JWT 换形象配置 → 200 | ✅ | 真库 + 真 app：`POST /api/device-sessions` 200 → `GET /api/pad/avatar-config` 200，`avatar_url=https://localhost/avatars/vrm1_sample.vrm`、`version=1686351657` |
| 不带凭证 → 401 | ✅ | 同一 app `GET /api/pad/avatar-config` → 401 `TOKEN_INVALID` |
| 设备未绑长者 → 403 `device_not_bound` | ✅ | `tests/test_pad_avatar.py` 覆盖，错误体与 elders/devices 同形 `{detail, code, trace_id}` |
| 三层取值链逐层降级 | ✅ | 单测 14 例：persona 相对路径→绝对、绝对 URL 直通、空 persona 落 Settings、两层皆空返 `null` 仍 200 |
| 改 persona → `version` 变化、零代码换形象 | ✅ | 实测 `avatar_url` 改 `/avatars/xiaoman.vrm` → `version` 由 `1686351657` 变 `1794577085`，改回后**确定性还原**为 `1686351657`（crc32 指纹打在已绝对化的响应值上，改 `avatar_url` 或 `PUBLIC_BASE_URL` 都会变） |
| `.vrm` 经 nginx 可拉取、MIME 正确 | ✅（配置级） | `docker run --rm -v .../nginx.conf:...:ro nginx:alpine nginx -t` → `syntax is ok / test is successful`；`types { model/gltf-binary vrm; }` 已显式声明 |
| persona seed 幂等 | ✅ | 连跑两次：第一次 `inserted`，第二次 `kept (token_hash 不覆盖)`；`scripts/seed_personas.py` |
| 质量门 | ✅ | `429 passed, 1 skipped`（skip 为未装 edge-tts）；总覆盖率 **79.17%**（门槛 70%），`app/api/pad.py` 96%；`ruff check` / `format --check` 全绿 |

实现落点：`app/services/persona_service.py`（取值链与指纹）、`app/api/pad.py`（契约薄层）、`app/main.py`（挂载）、`scripts/seed_personas.py`、`deploy/nginx.conf`、`deploy/assets/`、`tests/test_pad_avatar.py`。

两条设计取舍（评审请重点看）：

- **未绑定一律 403，不返回 `200 + avatar_url=null`**。`null` 的语义是「后端没配形象，前端用内置兜底」；「设备没绑长者」需要人去照护台重新绑定，两者处置不同，合并会把运维缺口藏起来。
- **查不到长者行时降级不抛 404**（契约 C7 只定义 200/401/403），但打 `avatar_config_elder_missing` warning 带 `trace_id`，否则「设备绑了一个 elders 里不存在的 id」永远查不出来。

---



### 7.4 Day 6 · A 线（历史 / 工具 / 用药 / 工单）验收记录（2026-11-09）

**测试执行口径（重要，先看这条）**

```bash
uv run pytest -q -m "not slow" --cov=app --cov-report=term
```

必须带 `-m "not slow"`。`tests/test_audio_tts.py::test_edge_live_synthesis_over_network`
是真实 edge-tts 合成，**需要外网**；不带 `-m` 时它会因为本机无外网而失败，
看起来像「代码坏了」，实际是环境依赖（该用例自身已标 `slow` 并在 docstring 写明）。

| DoD 项 | 结论 | 取证 |
| --- | --- | --- |
| `pytest` 全绿 | ✅ | **931 passed, 4 skipped, 1 deselected**（`-m "not slow"`；deselected 即上面那条外网用例） |
| 覆盖率 ≥70% | ✅ | **TOTAL 81.51%**（`source=app`、`branch=true`，`fail_under=70` 未触发） |
| 迁移可回滚 | ✅ | `0007 → head`（25 表）→ `downgrade 0007`（15 表）→ 再 `upgrade head`（25 表），三次连续成功 |
| ORM 与迁移零漂移 | ✅ | `alembic check` 对本线三表与 `conversation_threads.last_event_seq` 零差异 |
| `ruff check app tests` 干净 | ✅ | `All checks passed!` |
| 本线 `ruff format --check` 干净 | ✅ | 本线 14 个文件 `already formatted`。**仓库全局仍有 13 个文件待格式化**，均属其它线的在研代码，未在本线批量重排（避免把无关 diff 混进提交） |
| 看板 / 计划索引一致 | ✅ | `update_progress.py --check` 与 `--check-plans` 均 0 退出码 |

**本线交付的模块与用例**

| 模块 | 文件 | 用例 |
| --- | --- | --- |
| 历史账本（N01 / N04） | `app/models/conversation.py`、`app/services/history_service.py` | `tests/test_history_store.py` **15 条**（含并发 20 写 `seq` 恰为 1..20、N07 先写事件时 commit 只补投影、读函数可省略 `session`） |
| 工具执行（N12 / N26） | `app/agents/tool_registry.py`、`app/services/tool_executor.py` | `tests/test_tool_executor.py` **20 条**（越权审计、四类失败、幂等三例、护栏预算、MCP 成功/超时/未授权/参数错/未接线） |
| 用药子图（N10） | `app/graphs/checkpointer.py`、`app/graphs/medication_confirmation_graph.py`、`app/services/medication_service.py` | `tests/test_medication_graph.py` **5 条**（含重启恢复不重发、四次触达 → `missed_unconfirmed`、终态不覆盖、漏服建单） |
| 工单（A12） | `app/services/work_order_service.py` | `tests/test_work_order_service.py` **15 条**（幂等、非法迁移、误报必带原因、红单必带处置时间） |
| 身份注入（复核项） | `app/agents/companion.py` | `tests/test_companion_identity.py` **4 条**（generic 不崩、明确身份带人设、推断留痕） |

**三条设计决定（评审请重点看）**

- **`seq` 用行锁分配**（`SELECT … FOR UPDATE` 锁 `conversation_threads` 行后 +1）。禁止「先查 `max` 再 +1」——那是竞态，并发下必然重号，而 `UNIQUE(thread_id, seq)` 一旦报错，整轮对话就丢了。
- **`read_turns` 按各轮 `turn_started` 的 `seq` 排序，不按 `started_at`**。`now()` 取的是**事务开始时间**，同一事务里建的两轮拿到完全相同的时间戳，按它排等于随机——上下文一错序，「刚才那个」就指到别处。
- **`assistant_delivered_text` 只存已送达文本**。生成全文留在 `conversation_events.payload_json`。把没播的内容写进投影，模型会以为自己说过。

**已知边界（不是缺陷，是明确留白）**

- 用药触达下发仍是占位（`TODO(E2)`），等 dev-C 的 `deliver()` + ack 接线；当前只记录「卡片已渲染」这一条触达。
- `work_orders` 有**两个建单入口**（本线的 `work_order_service` 与 dev-C 的 `safety_service`），登记技术债 **WD-1**；`work_orders` 缺 `source_type`/`source_id`，通用幂等靠「确定性单号」绕行，登记 **WD-2**。两条都随下一次改该表的迁移收敛。
- 全仓暂无 `WorkOrderTransition` 的第二处写入路径：安全层建单后不写流转，看板的「流转条数」目前只对漏服单准。属 dev-C 的文件范围。

## 8. 已知限制与降级矩阵

| 故障 | 表现 | 降级行为 |
| --- | --- | --- |
| 3D 模型加载失败或超 5s | 页面可能白屏 | 切静态头像 + 字幕，**不白屏** |
| WebRTC 建连超 5s / ICE 失败 / TURN 不可达 | 双向语音不可用 | 转 HTTP 按住说话整段上传 |
| ASR 不可用 | 语音无法转文本 | 转页面输入框 |
| Laya 加载失败 | System One 缺失 | 全部请求走 System Two，并打降级标记 |
| System Two 超时/限流 | 开放域回复失败 | 重试 2 次后返回「L1 结果 + 模板话术」并提示人工 |
| 天气 API 失败 | 天气查询空回 | 返回缓存值并标注数据时间；无缓存则如实告知 |
| 网络中断 | 页面不可用 | 前端 IndexedDB 兜底（P1） |
| 声纹置信不足 | 无法确认身份 | 主动询问「请问是哪位在说话？」；仍无法确认进通用模式（不读记忆、不写健康、不确认用药） |

**其他限制**：无真实平板硬件；天气是唯一真实外部系统，其余全 Mock；声纹向量维度与唤醒词引擎待 Day 3–4 定稿；单机 Docker 验收负载为 1 路活跃语音 + ≤10 后台用户。

---

## 9. 版权与合规

- 戏曲、影视、音乐素材**只使用教师提供的 20–30 条授权样例**，不爬取、不内置来源不明内容。
- 长者声纹样本由本人现场录制 3 段，录制前告知用途，**可随时要求删除**。
- 原始音频默认不保存（仅调试开关临时开启，最长 72 小时自动删除），**不得用于任何模型训练**。
- 日志与 Trace 上传前必须脱敏：无身份证、手机号、完整住址、密钥。
- 本项目为教学与演示用途，**不构成医疗器械或医疗服务**。

---

## 10. 文档索引

| 文档 | 用途 |
| --- | --- |
| [`AGENTS.md`](AGENTS.md) | **AI 协作规则**：技术栈、目录约定、常用命令、禁区、高风险文件清单、模型路由表、硬性数值 |
| [`docs/康伴3D数字人_PRD.md`](docs/康伴3D数字人_PRD.md) | 需求与验收基线（**v2.6**，§7 修订记录含 26 表 / source 四值 / `follow_up` / `rtc_failed` 的口径变更） |
| [`docs/康伴3D数字人_技术设计文档.md`](docs/康伴3D数字人_技术设计文档.md) | 架构、30 张表、Agent 闭环、子图设计（**v2.0**，附修订记录） |
| [`docs/ER_数据模型设计.md`](docs/ER_数据模型设计.md) | **数据模型唯一实现口径**（v0.5）：30 张表逐字段、约束与索引、迁移分批、42 条 AC 取证矩阵 |
| [`docs/工作流选型说明.md`](docs/工作流选型说明.md) | Workflow vs Agent 判定：四问决策法、9 类能力形态映射、两个子图的分支条件 |
| [`docs/prompts/seed-prompt.md`](docs/prompts/seed-prompt.md) | 种子提示词模板（六要素 + 两个实例） |
| [`docs/康伴3D数字人_大纲.md`](docs/康伴3D数字人_大纲.md) | 十天迭代流程（Day 1–Day 10）：课时划分与每日产出物。**仅作节奏参考，不作为需求与技术事实依据**（见 `AGENTS.md` 前置节） |

后续按课程进度补齐：`docs/HARNESS_DESIGN.md`、`docs/IDENTITY_RULES.md`、`docs/SECURITY.md`、`docs/AI_USAGE.md`、`docs/INTERVIEW_QA.md`、`app/prompts/personas/`（人设模板正文）。

---

## 11. 已知文档冲突（实现以 PRD 为准）

| 冲突点 | 旧口径 | 现行口径 |
| --- | --- | --- |
| 药单修改权限 | 大纲 §7.2：护工可改 | **PRD §4.5：医生增改停查，护工与管理员只读** |
| Checkpointer 后端 | 大纲 Day7：SQLite | **PostgreSQL 独立 schema `langgraph`** |
| 表数量与表名 | 大纲 11 张（`workorders`/`conversations`） | **30 张（技术文档 26 张 + `metrics` + Day 6 三张事件表 `conversation_events`/`conversation_turns`/`tool_executions`）** |
| 老电影点播 | 大纲列为功能验收 | **PRD 降为 P2** |
| 通知渠道 | 旧稿含微信 / 邮件 | **仅站内 `inapp` + Web Push** |
| 前端 UI 库 | 技术文档 §1.4 概括为 Tailwind | **pad 用 Tailwind，console/family 用 MUI** |
| Day 5 metrics 表 | 大纲 5.2 口径含「metrics 表」 | **技术文档 §3.6 原批次无此表（26 张）；2026-11-07 主理人裁决 D2 自裁追加第 27 张表 `metrics`（分段耗时打点），走新迁移 0006** |
| 口型同步实现 | 大纲 5.1 口径为音素级 viseme | **PRD FR-3D-01：TTS 音频能量近似驱动口型开合即可（day-05 计划裁决 D1 维持现状，不升级 viseme）** |
| Day 6 主题 | 大纲 §十 / PRD 旧口径：三级路由（规则层 + Laya + MCP） | **现行：长者侧 Agent 完整闭环（历史/状态/上下文/送达/记忆骨架/用药子图 + 天气 MCP + AC-04 安全一票否决）；三级路由、`rules.py`、Laya、`decision_logs` 顺延至路由日**（2026-11-09 裁决，PRD v2.9 / TD v2.0） |
| Day 8 声纹调优归属 | 大纲 8.4 + PRD §4 FR-VPR-04 表行（「Day 8.4 产出报告」）：声纹调优在 Day 8 | **现行：主理人 2026-11-10 晚改裁决 D-1——声纹调优（FR-VPR-04）纳入 Day 8 以 P1（H13）承载，产能不足仍可顺延 Day 9**；PRD §6.1 Day 9 行与 TD §8.4.3 的「归 Day 9」表述待同步修订（见 docs/plans/day08/day-08 计划 §0 / §10） |
| Day 8 护工看板渲染形态 | 大纲 8.2：最简 HTML+JS 服务端渲染，不引前端框架 | **PRD §2.1：console 统一 React + 原生 CSS（tokens.css 唯一真源，MUI 已于 2026-11-06 移除），在现有工程内真实对接，不另起服务端渲染**（2026-11-10 主理人裁决 D-3） |
