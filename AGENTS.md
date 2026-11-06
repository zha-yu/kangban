# AGENTS.md · 康伴（SilverMate）工程协作规则

> 本文件是**跨工具通用**的项目规则（Cursor / Claude Code / Codex CLI / Windsurf / Trae / 通义灵码 / Copilot 等读取同名文件），不绑定任何一款 AI 编程工具。
> 本文件只写**规则与约束**，不写教程。教程与方案在 `docs/`。
> 冲突时的效力顺序：`docs/康伴3D数字人_PRD.md`（验收基线） > `docs/康伴3D数字人_技术设计文档.md`（实现口径）。**大纲不在事实效力链上**，见下节。

---

## 前置 · 文档角色与事实来源（最高效力，先读这条）

`docs/` 下三份基线文档**不是并列的资料**，各自只能提供一类信息。**大纲只提供迭代流程，不提供任何事实依据。**

| 文档 | 唯一可提供 | 明确不可提供（引用即错） |
| --- | --- | --- |
| `docs/康伴3D数字人_PRD.md` | 需求、验收标准（`FR-*`）、业务规则、状态机、幂等口径、枚举取值、业务边界与非目标、Mock 边界 | 目录结构、依赖清单、DDL 细节、迁移批次 |
| `docs/康伴3D数字人_技术设计文档.md` | 架构与分层、目录结构、26 张表的字段/类型/约束/索引、技术选型、Agent 与子图设计、迁移批次、trace 与可观测 | 验收条件、业务规则（只做实现映射，不做业务裁决） |
| `docs/康伴3D数字人_大纲.md` | **仅**十天迭代流程：Day 1–Day 10 的课时划分、每课「本课做出来」的产出物、交付与验收的先后顺序 | **需求、数据模型、表名与字段、接口口径、技术选型、数值阈值、UI 口径**——一句都不要引用 |

**铁律（违反即返工）**

1. 回答或实现任何「是什么 / 有多少 / 什么字段 / 什么接口 / 什么选型」的问题，**只能引用 PRD 与技术设计文档**；不得拿大纲正文当答案，也不得把大纲里的方案当默认值。
2. 大纲只回答「下一步做什么、按什么顺序做、今天该交付什么」。排期与产出物可以引用大纲，事实一律回到前两份文档。
3. 大纲与 PRD / 技术文档冲突时，**直接以 PRD 与技术文档为准**，不把大纲当第三方意见去平衡；已发现的冲突只登记在 README「已知文档冲突」表，不允许反复翻旧稿重新裁决。
4. PRD 与技术文档互相矛盾时，按「PRD 定验收、技术文档定实现」拆分；拆不开的回报主理人裁决，**不自行选一个**。
5. **已作废的大纲口径（出现即错）**：`workorders` / `conversations` 那套 11 张表、SQLite checkpointer、护工可改药单、AI 歌曲生成。以技术文档 26 表、PostgreSQL 独立 `langgraph` schema、PRD 药单权限为准。
6. **引用大纲「十、迭代计划」时，只取流程、不取内容**：可以引用「Day N 做哪几件事、当天产出物、先后顺序」；**不得**把课时正文里的表名、字段名、接口路径、请求/响应结构、算法、阈值、页面口径抄进代码或契约当依据。要落地成字段或接口，回 PRD 与技术设计文档取；两份都没有的，按 `AGENTS §10` 记自裁并与代码同提交留痕，不许说「流程里就是这么写的」。

---

## 0. 项目一句话定位

面向 CCRC 康养社区长者卧室的 **3D 数字人陪伴系统**：浏览器页面（模拟平板）上的 VRM 数字人 + WebRTC 全双工语音，用「安全规则层 → Laya 端侧分类器 → 主控 Agent」三级路由，承担情感陪伴、用药提醒、健康数据采集、戏曲/天气检索与人工告警五类职责。**不做医疗诊断，不替代 SOS 紧急呼叫。**

三个使用端同一仓库存放：长者端 `pad`（1920×1080 大字）、照护台 `console`（护工/医生/管理员按角色分流）、家属端 `family`（375px 移动优先）。

---

## 1. 技术栈

| 层 | 选型 | 口径说明 |
| --- | --- | --- |
| 语言 | Python 3.12（文档要求 3.12+） | 异步优先；`.python-version`、`requires-python`、ruff `target-version`、mypy `python_version` **四处已对齐 3.12**，改版本必须同改四处 |
| HTTP | FastAPI + Uvicorn + Pydantic v2 + Pydantic Settings | `.env` 由 Pydantic Settings 加载；`trace_id` 中间件挂在 `app/main.py` |
| 前端 | React + Vite（**MPA 三入口**）+ Tailwind（pad）/ MUI（console、family） | 单仓库单 React 工程，避免 three-vrm 进 console/family 构建包 |
| 3D 数字人 | three.js + `@pixiv/three-vrm` | 加载失败或超 5s 降级静态头像，**不白屏** |
| 实时语音 | WebRTC + aiortc + coturn + TEN-VAD（端侧主 VAD）/ webrtcvad（服务端兜底） | 信令复用 FastAPI WebSocket，不新建信令服务；按住说话整段上传为降级 |
| 语音识别/合成 | 第三方**流式** ASR / TTS API | 教师统一提供 key；必须带置信度埋点 |
| 主库 | PostgreSQL 15 + pgvector | 业务表与 LangGraph checkpoint **物理隔离**（独立 schema `langgraph`、独立连接池） |
| ORM/迁移 | SQLAlchemy 2.x（async）+ Alembic | 分批迁移，**主迁移一经合入不再改** |
| 缓存 | Redis | 短期会话、限流、重复调用检测；**不是长期记忆的唯一存储** |
| 调度 | APScheduler | 用药到点、19:00 健康采集、日报、记忆归并、漏采派单、原始音频 72h 清理 |
| System One | Laya（本地开源权重，CPU 推理） | 一次前向出 intent / sensitive / confidence / privacy 四信号 |
| System Two | DeepSeek 或 Qwen 兼容 API | 经统一 LLM 适配器接入，不直连 SDK |
| Agent 编排 | 受约束 CompanionAgent（ReAct + Tool Calling） | 业务子图仅 2 个：`medication_confirmation_graph`、`health_checkup_graph` |
| 状态工作流 | LangGraph + PostgreSQL Checkpointer | 按 `thread_id` 持久化，支持 Interrupt/HITL 与断点恢复 |
| 工具协议 | MCP | `weather`（唯一真实外部接口）+ `mock-operations`（工单/通知，Mock 落库） |
| 观测 | Langfuse + OpenTelemetry Traces + `/metrics` | 全链路统一 `trace_id`；上传前脱敏 |
| 容器 | Docker Compose + nginx | 7 服务一键起；HTTPS 本地受控证书（getUserMedia 硬要求） |

---

## 2. 目录约定

目标结构（技术设计文档 §2.1，**新代码一律按下表落位**）：

```
.
├── app/                        # 后端唯一代码根（不叫 src/）
│   ├── main.py                 # FastAPI 装配、/health、trace_id 中间件
│   ├── core/                   # 配置、JWT 三体系（人/设备/工牌）、鉴权依赖、runtime_context、脱敏
│   ├── api/                    # REST 路由：只做参数校验 + 权限范围过滤 + 调 services
│   ├── audio/                  # WebRTC 建连与媒体、反向外呼、上传降级、WS 信令、回执、断线检测
│   ├── router/                 # safety.py（安全一票否决）+ rules.py（规则层 ≤10 条）
│   ├── system_one/             # Laya 客户端封装、questions.py（四信号）
│   ├── agents/                 # companion.py（ReAct Loop）、预算熔断、工具注册表
│   ├── graphs/                 # 两个业务子图 + checkpointer（独立 schema）
│   ├── services/               # 领域服务：唯一写库层
│   ├── models/                 # SQLAlchemy 2.x ORM（26 张表）
│   ├── mcp_client/             # 工具发现/调用/超时/越权审计
│   ├── scheduler/              # APScheduler 任务注册
│   └── prompts/personas/       # 人设 prompt（进 Git 版本管理，固定前缀保 cache）
├── mcp-servers/
│   ├── weather/                # get_weather(city)
│   └── mock-operations/        # create_work_order / send_family_notification
├── frontend/
│   ├── index.html              # MPA 入口声明
│   └── src/{pad,console,family}/
├── alembic/versions/           # 分批迁移脚本
├── eval/fixtures/              # 固定测试集（普通 50/敏感 20/边界 20/模糊 20/注入 ≥10）
├── eval/reports/               # 基线与 bad case 回流报告
├── tests/                      # test_gates.py / test_injection.py / eval_gate.py
├── spikes/                     # 新依赖最小验证，跑通才合并
└── docs/                       # PRD、大纲、技术设计、ER、HARNESS 等基线文档
```

**分层硬约束（违反即返工）**

1. `app/services/` 是**唯一写库层**。`api` / `audio` / `scheduler` / `graphs` 一律不直接操作 ORM 写数据。
2. 依赖方向单向：`api | audio | scheduler | graphs → services → models`。**任何模块不得横向 import**（如 `api` 直接 import `graphs`）。
3. `app/api/` 不写业务逻辑，只做校验、范围过滤、调服务、序列化响应。
4. 前端三入口共用一套 Vite 配置，**pad 的 three-vrm 依赖不得进入 console/family 的构建产物**。

---

## 3. 常用命令

```bash
# 依赖（包管理器固定 uv）
uv sync                        # 按 pyproject + uv.lock 安装
uv add <pkg>                   # 新增依赖；新依赖先写进 spikes/ 验证再合并

# 本地依赖服务（只起基础组件，后端本地跑便于调试）
docker compose up -d postgres redis

# 后端
uv run uvicorn app.main:app --reload --port 8000
curl -s localhost:8000/health

# 迁移（autogenerate 后必须人工读 diff 再 upgrade）
uv run alembic revision --autogenerate -m "add_xxx"
uv run alembic upgrade head

# 测试与门禁
uv run pytest -q
uv run pytest --cov=app --cov-report=term-missing
uv run python tests/eval_gate.py          # 评测门禁，pre-push 钩子同款

# 前端
pnpm --dir frontend install
pnpm --dir frontend dev                   # 本地开发（HTTPS or localhost）
pnpm --dir frontend build                 # 产出 MPA 多页，交给 nginx 托管

# 全栈
docker compose up --build
```

> 端口约定（文档未定义，本项目自定义 **并已回填 README**）：postgres 5432、redis 6379、backend 8000、frontend nginx 80/443、laya 7001、weather-mcp 7101、mock-operations-mcp 7102、coturn 3478/5349、vite dev 5173。

### 3.1 验证路径（uv 优先，Docker 兜底）

**Windows 侧有 `uv`**：`C:\Python313\Scripts\uv.exe`（实测 0.12.24），已装 cpython 3.12.13 / 3.13.7 / 3.14.x。**首选**在 PowerShell 里直接 `uv sync` / `uv run ...`，`.python-version` 已钉 3.12，`uv sync` 会把 `.venv` 对齐到 3.12。

**WSL 里没有 `uv`**，`.venv` 与系统 `python3` 也没有 `pip` —— **在 WSL 里跑 `uv run pytest` 会落空**，从 WSL 调 Windows PowerShell 即可（实测可用）：

```bash
powershell.exe -NoProfile -Command "Set-Location 'E:\project_month\silvermate'; uv run pytest -q"
```

> 注意：`wsl` 工具会自动把命令里的 Windows 路径改写成 `/mnt/e/...`，交给 `powershell.exe` 就会变成非法路径（报 `E:\mnt\e\...` 不存在）。调 PowerShell 时必须关闭路径改写（工具参数 `translatePaths: false`），或干脆不要写 Windows 绝对路径。

若某台机器连 `uv` 都装不上，再用 Docker 跑同一批命令（已验证可行）：

```bash
# 一次性：起验证容器（仓库根挂到 /w），装与 pyproject 对应的依赖
docker network create silvermate-verify
docker run -d --name silvermate-pg-verify --network silvermate-verify \
  -e POSTGRES_USER=silvermate -e POSTGRES_PASSWORD=silvermate -e POSTGRES_DB=silvermate \
  pgvector/pgvector:pg16
docker run -d --name silvermate-app-verify --network silvermate-verify \
  -v "$PWD":/w -w /w python:3.12-slim sleep infinity
docker exec silvermate-app-verify pip install fastapi "uvicorn[standard]" pydantic-settings \
  sqlalchemy alembic "psycopg[binary]" pytest pytest-asyncio httpx ruff

# 每次改完代码
docker exec -w /w silvermate-app-verify sh -c 'PYTHONPATH=/w ruff check app tests && ruff format --check app tests'
docker exec -w /w silvermate-app-verify sh -c 'PYTHONPATH=/w python -m pytest -q'
docker exec -w /w -e DATABASE_URL=postgresql+asyncpg://silvermate:silvermate@silvermate-pg-verify:5432/silvermate \
  silvermate-app-verify sh -c 'PYTHONPATH=/w alembic upgrade head && PYTHONPATH=/w alembic current'

# 起服务验证接口（slim 镜像没有 curl，用 python 或 apt-get install curl）
docker exec -d -w /w silvermate-app-verify sh -c 'PYTHONPATH=/w exec uvicorn app.main:app --port 8000 >/tmp/uv.log 2>&1'
docker exec silvermate-app-verify python -c "import urllib.request as u; r=u.urlopen('http://127.0.0.1:8000/health'); print(r.status, r.headers['X-Trace-Id'])"
```

> 坑（已踩）：`docker cp` 到容器前必须先 `docker exec <c> mkdir -p <dir>`，否则报
> `Could not find the file …`；`docker exec -w` 指向不存在的目录会报 `chdir to cwd … failed`。
> 装了 `uv` 之后以 `uv sync` 为准，本节降级为「无法装 uv 时的兜底」。

---

## 4. 禁区（红线，AI 不得自行决定）

1. **不连生产数据库、不连真实医疗系统**。只允许连 `docker compose` 起的本地 PostgreSQL。
2. **不硬编码任何密钥**：API Key、JWT secret、数据库口令、TURN credential 一律经 Pydantic Settings 从 `.env` 读取；`.env` 已在 `.gitignore`，`.env.example` 只放占位符。
3. **不把密钥、真实长者数据写进 prompt、测试夹具、日志或提交信息**。
4. **不说医疗诊断口径**（禁止「您这是高血压/糖尿病」）。异常值一律走「建议问医生」话术，并同步走规则层人工告警。
5. **不接真实微信 / HIS / 短信 / 电话 / 支付**。除天气 API 外，所有外部系统走 Mock。
6. **不改已合入的 Alembic 历史迁移**，只新增迁移文件。
7. **基线文档（`docs/` 下 PRD / 技术设计文档 / 大纲）可以改正文，但不许"静默改"**：任何口径变化必须①在**该文档文末的「修订记录」章节**新增一条，逐条写清「改了哪条、依据哪条裁决或 PRD 条款、影响哪些 FR/AC/表」；②正文改动与修订记录在**同一次提交**里。**修订记录章节不存在时必须先补建再写本次记录**（历史被误删、新文档尚未建立都算），补建格式：标题统一 `## 修订记录`（不再用「附 A/B/C」字母编号），三列表 `| 版本 | 日期 | 说明 |`，版本号沿用该文档现有最大值 +1，无历史则从 `v1.0` 起。**禁止**：删除或回填历史修订行、把旧版本数字悄悄改掉却不留痕、改完正文不写记录、以「文档里没有这一节」为由跳过记录。
8. **不新增第二种同类能力实现**：一个能力只允许一种形态（急症=规则层 Workflow、天气=MCP Tool、用药=LangGraph 子图……），已定形态不得并行造第二套。
9. **不允许模型自报身份**：`elder_id` / `thread_id` 只能从服务端 `runtime_context` 注入，客户端与模型传入的值一律丢弃。
10. **不落未经脱敏的原始音频与个人敏感信息**；原始音频最长保留 72h 且只允许由调试开关临时打开。
11. **不提交** `node_modules/`、`.venv/`、媒体大文件、原始录音、`*.vrm`/`*.glb`。
12. **不擅自升级主力依赖或引入新框架**（换 ORM、换 LLM SDK、加状态库均需先讨论）。

---

## 5. 高风险文件清单（AI 生成后**必须人工逐行读 diff**才能合并）

| 文件 / 目录 | 风险类别 | 人工审查必须确认的点 |
| --- | --- | --- |
| `app/core/security.py`、`app/core/auth*.py`、`app/api/auth*.py` | **auth** | 密钥只从配置读；JWT 算法与过期固定（Access 2h / Refresh 7d / 设备会话 24h）；`token_version` 失效逻辑；密码不明文、不返给前端；三套凭证体系不串用 |
| `app/models/**`、`alembic/versions/**` | **迁移与 schema** | 字段语义与类型（日期、枚举取值、可空性）符合技术文档 §3；迁移可回滚；不删改历史迁移；UUID 主键与索引齐全；不写死生产库连接 |
| `app/agents/tool_registry.py`、`app/agents/companion.py`、`app/mcp_client/**`、`app/core/runtime_context.py` | **Tool 权限** | 工具白名单与参数 schema 校验；身份只从 runtime_context 注入；越权必须拒绝并写 `audit_logs`；`MAX_TOOL_CALLS=8` 与同 Tool 熔断生效；超时与失败四分类齐备 |
| `mcp-servers/weather/**`、`mcp-servers/mock-operations/**` | **MCP server** | 工具 schema 与官方协议一致；入参强校验；返回脱敏、错误不回显内部细节；无自由外网访问；Mock 必须落库可查 |
| `Dockerfile`、`docker-compose.yml`、`.dockerignore` | **部署** | 非 root 运行；密钥不进镜像层（构建期无 `.env`）；依赖锁定；健康检查；端口不裸奔公网；TURN 凭据从环境注入 |
| `app/router/safety.py`、`app/router/rules.py` | **安全一票否决** | 急症/敏感判定先于一切；规则条数 ≤10；阈值与分级写在代码而非 prompt；异常值同时触发人工告警 |
| `app/graphs/**`（含 checkpointer） | **状态与断点恢复** | checkpoint 落独立 schema、独立连接池；`thread_id` 服务端生成；Interrupt/HITL 分支不漏；重启后可恢复 |
| `.env.example` | **配置泄露** | 只有占位符，无任何真实 key / 内网地址 / 账号口令 |

> 另：老师抽查口径为 `auth` / `memory` / `mcp_client` 三处，评审前请优先自查这三处。

---

## 6. 模型路由表

请求固定按 **L0 规则层 → L1 System One → L2 System Two** 三级升序，**低层不得被高层覆盖**。规则优先级：合法性校验 > 急症/敏感安全规则 > 进行中用药/健康任务 > 固定意图 > 低置信澄清或 System Two > 记忆个性化。

| 层级 | 承担者 | 部署形态 | 延迟预算 | 负责什么 | 不可用/低置信时降级 |
| --- | --- | --- | --- | --- | --- |
| **L0 规则层** | `app/router/safety.py` + `app/router/rules.py`（关键词/正则 ≤10 条） | 进程内纯代码 | 路由 ≤200ms | 安全敏感**一票否决**前置；高频固定意图直路由（换一段/大声点/小声点/停/越剧京剧/几点了/今天天气/我要吃药/再见/重复一遍） | 不存在降级：规则层失效即 P0 缺陷 |
| **L1 System One** | **Laya** 本地开源权重（CPU 推理） | 本地进程/容器，OpenAI 兼容 HTTP | 单句 ≤200ms | 七类 intent（闲聊/用药查询/戏曲点播/电影点播/天气查询/健康采集/其他）+ `is_sensitive` + `confidence` + 双人间隐私收敛 + `is_out_of_domain` | `confidence ≥ 0.85` 直接路由；`< 0.85` **只澄清一次**，仍模糊升级 L2；模型加载失败 → 全部走 L2 并打降级标记 |
| **L2 System Two** | **DeepSeek / Qwen 兼容 API**，经统一 LLM 适配器 | 云 API | 首 token ≤800ms | 开放域闲聊、多工具 ReAct 编排、结构化输出、记忆摘要 | 超时/限流 → 重试 2 次后退化为「L1 结果 + 模板话术」，并提示人工；不得自动执行高危写操作（改药单/关工单/删数据） |
| **语音识别** | 第三方**流式 ASR** API | 云 API（教师统一 key） | 首包 ≤300ms | 方言语音转文本 + 置信度埋点 | 降级：按住说话整段上传 → 再降级：页面输入框 |
| **语音合成** | 第三方**流式 TTS** API | 云 API | 首包 ≤300ms | 语音播报 + 口型能量近似 | 降级：大字幕 + 预置音频；断网可用浏览器 `speechSynthesis` |
| **Embedding / 声纹** | 向量模型（pgvector 存储）+ 3D-Speaker CAM++ | 本地或 API | 不计入主链路 | 长期记忆召回（强制 `WHERE elder_id`）、声纹比对（cosine ≥0.85） | 召回失败 → 本轮不加载记忆，**不得臆造**；声纹不可用 → 长者 ID + 主动确认 |

**总预算**：端到端 P95 ≤ 1.5s（ASR + 路由 + 生成 + TTS 合计）。
> 上表逐层毫秒数为按 1.5s 端到端目标拆分的工程预算，非 PRD 明文数值；调整必须同步 `docs/HARNESS_DESIGN.md`。

---

## 7. 硬性数值（写死在常量里，不得私自调整）

| 参数 | 值 | 出处 |
| --- | --- | --- |
| `MAX_TOOL_CALLS_PER_TURN` | 8 | PRD FR-DLG-10 |
| 同一 Tool 熔断 | 1 分钟内第 3 次调用 | PRD FR-DLG-10；技术文档 §6.2 |
| 快决策置信度阈值 | 0.85（低于则澄清一次后升级） | PRD §2.3 |
| 声纹判定阈值 | cosine ≥ 0.85 | PRD §3.2 |
| 用药触达 | T0 / +5 / +10 / +15 分钟，每次等 60s | PRD §4.5 |
| 健康采集窗口 | 19:00 起，20:00 强制结束 | PRD §4.6 |
| 健康红标 | 收缩压 ≥180 或 舒张压 ≥120（单分量即判红） | PRD §4.6 |
| 原始音频保留 | 72 小时后自动删除 | 技术文档 §1.2 第 8 条（PRD §1.5 / FR-TC-07） |
| 3D 加载降级 | 失败或 >5s 切静态头像 | PRD FR-3D-02 |
| 简单指令语音响应 | ≤1.5s（20 次中 ≥19 次达标） | PRD §5.3 |
| 固定意图路由 | ≤200ms（不含 ASR/TTS/外部接口） | PRD §5.3、FR-DLG-02 |
| 测试覆盖 | 核心 Tool 与权限场景 100%，接口 ≥70% | PRD FR-TC-06 |
| 规则层规模 | ≤ 10 条（只做减法，含播放控制指令集） | 技术文档 §1.1、§1.3 |

---

## 8. 编码与协作约定

- **提交**：Conventional Commits（`feat/` `fix/` `docs/` `refactor/` `test/` `chore/`），一次提交只做一件事，正文中文说清「为什么」。
- **类型**：全量 type hint；I/O 一律 async；配置走 Pydantic Settings，禁止 `os.environ.get` 散读。
- **日志**：结构化，**每一行都带 `trace_id`**；禁止打印密钥、手机号、完整住址、身份证；脱敏工具集中放 `app/core/`。
- **时间**：存储一律 UTC，展示层转 `Asia/Shanghai`。
- **幂等**：用药提醒键 `(resident_id, plan_id, scheduled_at)`，触达键 `(reminder_id, attempt_no)`；重复请求返回原结果，同键异内容返回 409。
- **失败分类**（工具层四类）：参数错 → 回传模型重试并计入预算；目标不存在 → 如实告知不编造；临时错误 → 退避重试 ≤2 次后降级话术；越权 → 拒绝 + 审计 + trace_id。
- **给 AI 下任务的统一模板**：`改哪些文件 → 验收命令 → 禁止触碰的文件 → 完成后报告 diff 摘要`。涉及 §5 清单内文件时，**先出方案再落代码**。
- **三次法则**：同一个坑第 3 次出现，必须补进下面 §9，否则不许继续往下做。

---

## 9. 踩坑记录（三次法则 · 同坑第 3 次必须写进来）

<!-- 格式：日期 | 现象 | 根因 | 铁律。没有第 3 次就先别写，避免变噪音。 -->

| 日期 | 现象 | 根因 | 铁律 |
| --- | --- | --- | --- |
| — | （待积累） | — | — |

---

## 10. 计划与文档产出（写在哪、叫什么）

**所有计划一律落盘到 `docs/plans/`**：不在对话里留计划，不把计划塞进 PRD / 技术设计文档正文，不新建 `plan.md` / `todo.md` 这类游离文件。

### 10.1 命名规范

`docs/plans/<类别>-<编号>-<主题>.md` —— 全小写 ASCII 前缀 + 中文主题，不含空格、斜杠、版本号。

| 类别 | 编号 | 例 | 什么时候用 |
| --- | --- | --- | --- |
| `day` | 大纲的 Day 序号，**两位** | `day-02-身份与权限.md` | 当天的多步计划、五人分工 |
| `task` | `docs/team_tasks/` 卡序号，**两位** | `task-03-库与模型基座.md` | 单张卡的实施计划 |
| `adhoc` | `YYYYMMDD` | `adhoc-20261104-契约冻结.md` | 一次性方案、评审记录、排查计划 |

- 主题 ≤ 12 字，与对应 task 卡的标题保持一致。
- **日期不进文件名**（`adhoc` 除外）：计划会跨天修订，日期前缀会逼你改文件名或复制出第二份；时间信息交给 git。
- 编号补零到两位，保证 `day-02` < `day-10` 字典序正确。
- **按天协作时，个人实施计划放 `docs/plans/day<NN>/` 子目录**（如 `docs/plans/day04/a1-声纹身份.md`），小写 ASCII 代号 + 中文主题；全量方案仍用平铺的 `day-NN-主题.md`，两类都须同一次提交登记进 `docs/plans/README.md`。
- 废弃的计划把文件头改成 `状态：已废弃`，**不删**（复盘要看）。

### 10.2 文件头必填四行

```markdown
> 状态：草稿 / 进行中 / 已冻结 / 已废弃　｜　版本：v1.0　｜　日期：YYYY-MM-DD
> 关联：docs/team_tasks/task-03-库与模型基座.md
> 上游依据：PRD line xxx（验收）｜技术设计文档 line xxx（实现）
> 变更记录见文末
```

### 10.3 索引

`docs/plans/README.md` 维护一张表（文件 / 一句话场景 / 状态 / 最后更新）；**新增计划必须同一次提交里加进表里**，否则等于没写。

### 10.4 四类文档的分工（别互相越界）

| 位置 | 只回答 | 改动成本 |
| --- | --- | --- |
| `docs/team_tasks/` | 谁做、前置、写作用域、验收 | 低（状态随认领更新） |
| `docs/plans/` | 怎么做、分几步、谁负责哪步 | 低（改文件内变更记录） |
| `docs/接口契约_*.md` | 对外口径：字段、判定、端点 | 高（走裁决 + 变更记录） |
| 三份基线文档 | 事实与验收 | 高（正文可改，必须同提交补修订记录） |

### 10.5 进度看板自动刷新（不用手写）

**逐日一份**：每个 `docs/team_tasks/day<N>/` 目录下都有一份**自动生成**的 `PROGRESS.md`（如 `docs/team_tasks/day03/PROGRESS.md`），内容 = 该日任务卡状态的汇总 + 计划件状态 + 契约冻结状态。脚本自动发现 `docs/team_tasks/day*/` 目录，**新开一天只需建目录 + 放任务卡**，不需要改脚本或钩子。

- **不要手改进度文件**，它会被下一次生成覆盖。要动进度，改任务卡的 `> 状态：` 行。
- 生成器：`scripts/update_progress.py`（纯标准库）。`--check` 只检查是否过期；`--day 4` 只算某一天。
- **自动触发**：仓库带 `core.hooksPath = .githooks`，`pre-commit` 钩子每次提交都会重算**所有**日看板并把变化连同本次提交一起入库；生成失败则中止提交（宁可不提，也不留过期看板）。新 clone 后跑一次 `git config core.hooksPath .githooks` 即可。
- 任务卡状态取值固定为 `未开始 / 进行中 / 已完成 / 已取消 / 待确认`，脚本据此上图标；不要自创状态词，否则图标会落成 `❔`。写法两种都认：引言行 `> 状态：已完成`，或表格行 `| 状态 | ✅ 已完成 |`（Day 2 用的是后者）。