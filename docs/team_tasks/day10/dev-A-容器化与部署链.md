> 状态：已完成（A1–A5 全部交付，确认点❺ 2026-11-14 主理人确认收口；M6.1 A 侧待与 dev-C 合验；R15/R16 裁决与任务书 §6 回填等遗留见计划件 v2.4）　｜　版本：v1.1　｜　日期：2026-11-14
> 负责人：**dev-A（唯一）**　｜　任务包：**A · 容器化与部署链**　｜　容量：上限 24h（3 人日）；实排开发 **17h ＝ 2.125 人日**，余额为 Line 1 联调槽位占用＋缓冲
> 关联：docs/team_tasks/day10/day10-任务总览与DAG.md v1.0（§2 DAG、§3 对接点 Y1/Y4、§4 写域）｜docs/plans/day10/day-10-交付与观测-工作推进计划.md v1.0 §4.1
> 上游依据：PRD §2.5（一键启动/HTTPS/健康检查）、line 578（Day 10 完成定义）、line 444（FR-ADM-10 设备掉线检测，P2）｜技术设计文档 §8.3 Day 10（多阶段非 root、七服务编排、回滚演练）
> 变更记录见文末

---

# dev-A · Day 10 任务书 · 容器化与部署链

## 0. 你的边界（先读这条）

### 0.1 写域仅限（越界即返工）

```
Dockerfile                            （新增，根目录）
.dockerignore                         （新增）
docker-compose.yml                    （增量，本包独占）
.env.example                          （增量；langfuse 段由 B 先定稿，按 B→A 顺序合入）
deploy/nginx.conf                     （增量，本包独占）
app/scheduler/device_jobs.py          （新增；不碰 dev-B 既有调度文件）
app/services/device_service.py        （增量）
tests/test_device_offline.py          （新增）
```

### 0.2 禁触（他人写域，需改动时提需求不直接改）

`frontend/**` 与 `frontend/frontend.Dockerfile`（dev-C）、Langfuse 接入代码与 `app/api/metrics.py`（dev-B）、`tests/eval_gate.py` 与 `.githooks/`（dev-D）、`README.md`（dev-C 独占——你的演练记录/门禁清单以「章节素材小抄」交 C 合入）、docs 交付文档三件（C）。`app/api/audio.py` 的 ICE 字段**只读**（day09 D2.5 已交付，字段不改名）。

### 0.3 高风险文件（AGENTS §5：先出方案再落代码＋人工逐行读 diff）

`Dockerfile`、`docker-compose.yml`、`deploy/nginx.conf`。三处必须先出方案（写在本文件 §5 执行记录）经确认后才落代码；证书文件**不进 git**，`.gitignore` 登记。

### 0.4 角色口径

本包**不含**独立测试/产品/设计角色工作。`tests/test_device_offline.py` 是你作为该功能开发的自测内嵌交付物（沿 day09 口径）。

### 0.5 你的 Line 1 槽位（优先级高于本包）

D10 穿插 X3/X4/X12 联调槽（15:00/16:00，day09 §3.4）。冲突时 Line 1 优先。

---

## 1. 节点 A1 · 后端 Dockerfile（4h）

**前置**：M6.0（D10 09:30）。**D10 第一个开工项。**
**对接**：产出镜像给 A2（build 源）。

### 要做什么

1. 多阶段构建：builder 阶段 `uv sync` 装依赖 → runtime 阶段仅复制 `.venv` + `app/` + `alembic/`。
2. 非 root 用户运行。
3. 入口用容器内 Linux 默认事件循环（uvicorn 标准启动即可；**Windows 方案 A 仅本机适用，Dockerfile 内不引入 policy 代码**）。
4. `.dockerignore` 覆盖 `.env`、`frontend/`、`node_modules`、`.workbuddy/`、`static/media` 大文件。

### 交付物与自检校验标准

| # | 交付物 | 自检判据（逐条留证据） |
| --- | --- | --- |
| 1 | `Dockerfile`、`.dockerignore` | `docker build` 成功；容器内 `id` 非 root；`docker run --rm <img> python -c "import app.main"` 无 ImportError |
| 2 | 密钥零进镜像层证明 | `docker history` 各层与 `.env` 无关（`grep -ri "SECRET\|PASSWORD" <layers>` 零命中）；镜像体积记录进 README 表（素材交 C） |

---

## 2. 节点 A2 · compose 完整版编排（5h）

**前置**：A1；软依赖 Y3（C 的 dist 路径，D11 上午到位）、Y2（B 的 langfuse 变量段，`.env.example` 按 B→A 顺序合入）。
**对接**：**提供 Y2**（langfuse 服务定义＋凭据注入方式）给 B1；产出全栈给 A3、B1 联调、D3、M6.1。

### 要做什么

1. 在既有 6 服务（postgres/redis/funasr/mock-operations-mcp/weather-mcp/voiceprint）基础上新增：`backend`（build A1，healthcheck=`curl /health`）、`nginx`（挂 `deploy/nginx.conf`＋前端产物）、`laya`（形态按总览 §8 Q7 裁决）、`coturn`、`langfuse`（Y2 契约）。
2. 全部服务 healthcheck ＋ `restart: unless-stopped`。
3. 端口沿用 README §6.2 既有约定（7001 laya / 7002 funasr / 7101 weather / 7102 mock-operations）。
4. `.env.example` 同步新增全部变量。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | `docker-compose.yml` 完整版、`.env.example` 增量 | `docker compose config` 校验通过 |
| 2 | 全栈健康证明 | `docker compose up -d` 后 `docker compose ps` 全部 `healthy`；`curl --noproxy '*' https://localhost/health` 返回 `status:ok` 带 trace_id |
| 3 | 密钥注入方式证明 | `docker image inspect <backend>` 的 Env 为空（密钥仅运行时容器注入，不落镜像层） |

---

## 3. 节点 A3 · nginx / HTTPS / coturn（3h）

**前置**：A2；消费 **Y1**（C 的三入口静态段草案，`deploy/nginx.conf` 由你独占合入）。
**对接**：产出 M6.1 的 HTTPS/信令/TURN 三项判据；D3 干净主机演练复用。

### 要做什么

1. `deploy/nginx.conf` 合入 Y1 静态托管段（三入口）＋ `/api`、`/ws` 反代 backend ＋ WebSocket upgrade 头。
2. HTTPS 受控开发证书（mkcert 或自签）；证书文件不进 git，`.gitignore` 登记；生成说明进 README §A4 章节（素材交 C）。
3. coturn 服务接入：`turn_url/turn_username/turn_credential` 与 day09 D2.5 的 `GET /api/audio/ice-config` 既有 config 字段对齐（**不改名**）。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | `deploy/nginx.conf` 增量、证书生成说明 | 浏览器打开 `https://localhost` 无证书告警（受控信任后） |
| 2 | coturn 编排 | `wss://` 信令建连成功（走 nginx 反代不断）；pad `RTCPeerConnection` 从 ice-config 拿到 TURN 地址 |
| 3 | 凭据红线 | `grep -rn "turn_credential" frontend/src` 零命中（沿 day09 D2.5 判据） |

---

## 4. 节点 A4 · 回滚演练 ＋ 部署门禁清单（2h，D12）

**前置**：A3。**对接**：**提供 Y4**（回滚基准 commit，登记进演练记录）给 D3/D5；产出素材给 C2（README 回滚章节）。

### 要做什么

1. 代码回滚：以 Y4 基准（当前 `8d506b2` 或 D13 前最后收口 commit）部署 → 故意改坏一行 → 回滚到基准并复跑 `/health` ＋ 一条 AC 冒烟（如 AC-01 天气）。
2. 数据回滚：`alembic downgrade 0020` 再 `upgrade head`，核对业务表数据不丢（**一次性数据卷**上演练；Day 10 零新增迁移，只执行不新建）。
3. 产出部署门禁清单六项（验收达成/测试通过/密钥安全/迁移可逆/监控就绪/回滚清晰），按风险分级，每项带「检查方法」列。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | 回滚演练记录（写入 README 回滚章节素材） | 两次演练均有操作日志与结果截图 |
| 2 | 数据回滚证明 | downgrade 后 `alembic_version` 回退一格、upgrade 后恢复且表数据完好 |
| 3 | 部署门禁清单 | 六项逐项有「检查方法」列；基准 commit 写入记录（Y4 闭环） |

---

## 5. 节点 A5 · 设备掉线检测 FR-ADM-10（3h，day09 顺延项 P2，D12）

**前置**：M6.0（软：与 B1 无文件冲突）。**对接**：无硬下游，D2 回归覆盖。

### 要做什么

心跳超 2 个周期判掉线 → `device.status='offline'` → 通知管理员与负责护工；掉线窗口与用药 T0 重叠时升级人工介入；设备自检异常（麦克风/网络）→ 工单。新增独立文件 `app/scheduler/device_jobs.py`，不碰 dev-B 既有调度文件（写锁例外已沿计划件确认原则）。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | `app/scheduler/device_jobs.py`（新）、`device_service.py` 增量 | 心跳超 2 周期 → status 置 offline 且 `notifications` 有行（收件人＝管理员＋负责护工）；恢复心跳回 online |
| 2 | 升级逻辑 | 掉线窗口与用药 T0 重叠 → 立即生成人工介入工单 |
| 3 | `tests/test_device_offline.py` | `pytest tests/test_device_offline.py` 全绿；非 admin/非负责护工查询掉线通知按既有权限矩阵 403 |

---

## 6. 执行记录（开工后填写）

| 日期 | 节点 | 结果/证据指针 | 等待项 |
| --- | --- | --- | --- |
| | | | |

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-13 | 首版，与总览 v1.0 同批产出。 |
| v1.1 | 2026-11-14 | **收口**：S0–S10 十一步全部完成，A1–A5 五节点交付（Dockerfile／compose 编排／nginx+证书+coturn／回滚演练+门禁清单／设备掉线检测），确认点❺ 主理人确认，A 包正式关闭。执行细节与证据指针见计划件 `docs/plans/day10/dev-a-容器化与部署链.md` v2.4 §8。 |
