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
| Python 3.13（要求 3.12+） | 后端运行时 | 仓库带 `.python-version = 3.13` |
| [uv](https://docs.astral.sh/uv/) | 唯一的 Python 包管理器 | 不要混用 pip / conda，本仓库 `.venv` 由 uv 管理 |
| Docker + Docker Compose | 起 PG / Redis / TURN 等组件 | 全栈一键起也用它 |
| Node.js + pnpm | 前端构建 | 三入口共用一套 Vite 配置 |
| mkcert（可选） | 本地 HTTPS 证书 | 麦克风在非 localhost 下必须 HTTPS |

> **Windows 不需要装 Microsoft C++ Build Tools**。VAD 依赖用的是 `webrtcvad-wheels`（上游 `webrtcvad` 的同源 fork，import 名与 API 完全不变），自带 cp310–cp314 预编译 wheel，`uv sync` 直接取二进制。若改回上游 `webrtcvad`，Windows 上会因缺少 C++ 编译器而 `uv sync` 失败。验证脚本：`uv run python spikes/vad/spike_webrtcvad.py`。

### 5.2 五步启动

```bash
# 1) 取代码
git clone <repo-url> && cd silvermate

# 2) 配环境变量（.env 已被 .gitignore 忽略，绝不入库）
cp .env.example .env
# 编辑 .env：至少填 LLM_API_KEY、ASR_*/TTS_*、WEATHER_API_KEY；其余本地可留默认值

# 3) 装依赖
uv sync

# 4) 起基础组件并建表
docker compose up -d postgres redis
uv run alembic upgrade head

# 5) 起后端与前端
uv run uvicorn app.main:app --reload --port 8000
pnpm --dir frontend install && pnpm --dir frontend dev
```

全栈一键起：`docker compose up --build`，然后访问 HTTPS 入口。

> **当前进度说明**：本仓库已完成 Day 1.2「搭工作台」（规则文件、依赖声明、配置样例、忽略规则与首次提交）。`app/`、`docker-compose.yml`、`alembic/`、`frontend/` 会在 Day 1.3–Day 10 逐步落地；上面的命令按目标形态编写，未就绪的步骤会随开发解锁。

---

## 6. 配置说明

所有密钥只走环境变量，由 Pydantic Settings 从 `.env` 加载；`.env.example` 只有占位符。**三份基线文档没有枚举任何环境变量名与端口**，下表是本项目自定义并回填文档的约定。

| 分组 | 变量 | 说明 |
| --- | --- | --- |
| 应用 | `APP_ENV` `DEBUG` `API_HOST` `API_PORT` `PUBLIC_BASE_URL` `FRONTEND_ORIGIN` | 运行环境、监听地址、对外域名 |
| 鉴权 | `JWT_SECRET` `JWT_ALGORITHM` `ACCESS_TOKEN_EXPIRE_MINUTES` `REFRESH_TOKEN_EXPIRE_DAYS` `ELDER_SESSION_EXPIRE_HOURS` | 人（2h/7d）、设备（24h）、工牌三套体系 |
| 数据库 | `DATABASE_URL` `LANGGRAPH_CHECKPOINT_DATABASE_URL` `LANGGRAPH_CHECKPOINT_SCHEMA` | checkpoint 必须落独立 schema `langgraph` 与独立连接池 |
| 缓存 | `REDIS_URL` | 短期会话、限流、重复调用检测 |
| System Two | `LLM_BASE_URL` `LLM_API_KEY` `LLM_MODEL` `LLM_TIMEOUT_SECONDS` | DeepSeek / Qwen 兼容端点，经统一适配器 |
| System One | `LAYA_ENABLED` `LAYA_BASE_URL` `LAYA_API_KEY` `LAYA_TIMEOUT_MS` `LAYA_CONFIDENCE_THRESHOLD` | 本地 CPU 分类器，阈值默认 0.85 |
| 语音 | `ASR_*` `TTS_*` | 第三方流式服务，教师统一发 key |
| 实时通话 | `TURN_URL` `TURN_USERNAME` `TURN_CREDENTIAL` `STUN_URL` `WEBRTC_ICE_TIMEOUT_SECONDS` | ICE 超时触发降级 |
| 天气 | `WEATHER_API_PROVIDER` `WEATHER_API_KEY` `WEATHER_API_BASE_URL` `WEATHER_CACHE_TTL_SECONDS` | 全系统唯一真实外部接口 |
| 通知 | `NOTIFY_CHANNEL` `WEBPUSH_VAPID_PUBLIC_KEY` `WEBPUSH_VAPID_PRIVATE_KEY` | 仅 `inapp` / `webpush`，**无微信、无邮件** |
| MCP | `MCP_WEATHER_SERVER_URL` `MCP_OPERATIONS_SERVER_URL` `MCP_CALL_TIMEOUT_SECONDS` | 工具发现与超时 |
| 观测 | `LANGFUSE_*` `OTEL_*` `METRICS_ENABLED` | Trace 上传前脱敏 |
| 数据保留 | `AUDIO_RETENTION_HOURS` `SCHEDULER_TIMEZONE` | 原始音频默认 72h 自动清理 |
| 前端构建期 | `VITE_API_BASE_URL` `VITE_PAD_WS_URL` `VITE_ENABLE_3D` `VITE_ENABLE_WEBRTC` | 以 `VITE_` 前缀暴露给浏览器 |

### 服务与端口

| 服务 | 端口 | 说明 |
| --- | --- | --- |
| postgres + pgvector | 5432 | 业务表 + 独立 schema `langgraph` |
| redis | 6379 | 缓存与限流 |
| backend（FastAPI + aiortc） | 8000 | `/health` 健康检查；WebSocket 信令 |
| frontend（nginx） | 80 / 443 | 静态资源 + HTTPS 入口 + 媒体直连 |
| laya | 7001 | 本地 System One 推理服务 |
| weather-mcp | 7101 | `get_weather` |
| mock-operations-mcp | 7102 | 工单 / 家属通知 |
| coturn | 3478 / 5349 | TURN/STUN，跨网段才需要 |
| vite dev server | 5173 | 仅本地开发 |

---

## 7. 质量门与验收

| 项 | 标准 |
| --- | --- |
| 核心 Tool 与权限场景 | 用例覆盖 **100%** |
| 接口测试覆盖率 | **≥ 70%** |
| 三道隔离闸门 | 会话隔离 / 记忆隔离（强制 `WHERE elder_id`）/ 工具授权，**100% 覆盖** |
| 提示注入测试集 | ≥10 条，**100% 拦截**；日志脱敏 grep 零命中 |
| 评测门禁 | `eval_gate.py` 作为 pre-push 钩子，固定集不达标阻断提交，bad case 回流 `eval/fixtures` |
| 端到端延迟 | P95 ≤ 1.5s；规则层路由 ≤200ms |
| 一键部署 | 全部服务健康检查通过 + 长者端 HTTPS 可访问 + 可回滚 |

硬性数值（`MAX_TOOL_CALLS=8`、0.85 阈值、用药四次触达、19:00–20:00 采集窗口、72h 音频保留等）在 [`AGENTS.md`](AGENTS.md) §7 有完整清单，**改动需走评审**。

---

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
| [`docs/康伴3D数字人_PRD.md`](docs/康伴3D数字人_PRD.md) | 需求与验收基线（v2.5） |
| [`docs/康伴3D数字人_技术设计文档.md`](docs/康伴3D数字人_技术设计文档.md) | 架构、26 张表、Agent 闭环、子图设计（v1.4） |
| [`docs/康伴3D数字人_大纲.md`](docs/康伴3D数字人_大纲.md) | 十天迭代流程（Day 1–Day 10）：课时划分与每日产出物。**仅作节奏参考，不作为需求与技术事实依据**（见 `AGENTS.md` 前置节） |

后续按课程进度补齐：`docs/ER_数据模型设计.md`、`docs/工作流选型说明.md`、`docs/HARNESS_DESIGN.md`、`docs/IDENTITY_RULES.md`、`docs/SECURITY.md`、`docs/AI_USAGE.md`、`docs/INTERVIEW_QA.md`、`docs/prompts/`。

---

## 11. 已知文档冲突（实现以 PRD 为准）

| 冲突点 | 旧口径 | 现行口径 |
| --- | --- | --- |
| 药单修改权限 | 大纲 §7.2：护工可改 | **PRD §4.5：医生增改停查，护工与管理员只读** |
| Checkpointer 后端 | 大纲 Day7：SQLite | **PostgreSQL 独立 schema `langgraph`** |
| 表数量与表名 | 大纲 11 张（`workorders`/`conversations`） | **技术文档 26 张（`work_orders`/`conversation_threads`）** |
| 老电影点播 | 大纲列为功能验收 | **PRD 降为 P2** |
| 通知渠道 | 旧稿含微信 / 邮件 | **仅站内 `inapp` + Web Push** |
| 前端 UI 库 | 技术文档 §1.4 概括为 Tailwind | **pad 用 Tailwind，console/family 用 MUI** |
