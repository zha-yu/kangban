> 状态：已完成（A1–A5 全部交付，确认点❺ 2026-11-14 收口；B 组遗留 2026-11-15 清零：R16 判据修订／R15 警示登记／§6 回填/coturn 清理；M6.1 A 侧待与 dev-C 合验；接线补丁待 dev-B §9.6）　｜　版本：v1.2　｜　日期：2026-11-15
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
| 3 | 凭据红线 | **凭据真值零命中**（R16 裁决 2026-11-15 修订，原字面判据 `grep -rn "turn_credential" frontend/src` 与 day09 D2.5 冻结实现冲突）：口令真值在 `frontend/src`、`frontend/dist`、模板占位均 0 命中（已实测），凭据仅由 ice-config 动态下发；`realtime.ts` 的字段名引用（L212/L220）允许 |

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
| 2026-11-13 | A1（S1+S2） | `.dockerignore` 放行 alembic；Dockerfile 两阶段+非 root（uid 1000）+`deploy/backend-entrypoint.sh` 自动迁移（SKIP_MIGRATIONS 开关）；自检五条全过（构建成功/非 root/import 无错/`docker history` 密钥零命中/Env 无密钥）；冒烟 `/health` ok 带 trace_id；镜像 **1010.4MB**（改 `COPY --chown` 优化 -841MB） | 无 |
| 2026-11-13 | A2（S3–S5） | compose 七服务编排（地址全服务名覆盖、backend 不映射宿主口）＋profile 矩阵四态（默认 7／local+funasr+voiceprint／langfuse v4 六服务／laya 容器，15 服务 config PASS）；backend/nginx healthy、迁移 0020→0021 实测；laya 按 Q7 兜底宿主 sidecar（7001 model_loaded=true）；共享口径：funasr/voiceprint/langfuse 连 dev-B | 无 |
| 2026-11-13 | A3（S6+S7） | `deploy/nginx.conf` 全量重写（resolver 127.0.0.11 动态重解析，后经换 IP 实证升格）；自签 CA＋受控信任（X509Chain=True、curl 不带 -k 200）；coturn 端到端（device token→ice-config 凭据同源→TURN Allocate success→external-ip 跨网段闭环、中继段 49160-49200）；M6.1 判据 `https://localhost/health` 200 带 trace_id 达成、wss 101 | 无 |
| 2026-11-14 | A4（S8） | Y4=`61759ca` 打注解标签 `y4-rollback-baseline`（接替任务书原拟 8d506b2）；代码回滚演练（ENTRYPOINT 破坏→502→25s 恢复→AC-01 冒烟 ok）；数据回滚演练（0021⇄0020，五表行数逐一致）；部署门禁清单六项（逐项检查方法+当日证据）落计划件 §9.4.4 | 无 |
| 2026-11-14 | A5（S9+S10） | `device_service.py` 增 detect_offline_devices／recover_online_devices／handle_device_self_check_failure（写库全走 service）；独立调度骨架 `app/scheduler/device_jobs.py`（未碰 dev-B 既有调度文件）；`tests/test_device_offline.py` 6 用例首跑即绿；全量 **1715 passed** 无回退 | main.py 接线补丁需求单交 dev-B（计划件 §9.6，约 2 行，D13 演示前合入即可） |
| 2026-11-14 | 确认点❺ | **A 包收口（主理人确认）**。五节点交付清单核对无误，HEAD `522b7d2` 推送 feature-zjj 真门禁 PASS | 无 |
| 2026-11-15 | §6 回填＋B 组 | 主理人裁决落档：**R16=A3 凭据红线判据修订为「凭据真值零命中」**（字段名命中允许，README §11 登记已知冲突，前端不改）；**R15=自定义 HTTPS 端口仅登记警示**（README 端口表加句，nginx.conf 不改）；coturn 残留警告清理（listening-ip/relay-ip/pidfile 新增，3 条 WARNING 消失，Allocate success 重验零回归，余 2 条证书 WARNING 为 no-tls 固有噪音）。本表即任务书 §6 回填（证据指针：计划件 §8 对应行） | 无 |

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-13 | 首版，与总览 v1.0 同批产出。 |
| v1.1 | 2026-11-14 | **收口**：S0–S10 十一步全部完成，A1–A5 五节点交付（Dockerfile／compose 编排／nginx+证书+coturn／回滚演练+门禁清单／设备掉线检测），确认点❺ 主理人确认，A 包正式关闭。执行细节与证据指针见计划件 `docs/plans/day10/dev-a-容器化与部署链.md` v2.4 §8。 |
| v1.2 | 2026-11-15 | **§6 执行记录回填**（A1–A5＋确认点❺＋B 组裁决七行）；A3 判据按主理人 R16 裁决修订为「凭据真值零命中」（字段名命中允许）；R15 端口警示登记口径落 README 素材（计划件 §9.2）。 |
