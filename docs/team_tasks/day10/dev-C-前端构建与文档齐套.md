> 状态：进行中（C1–C5 已交付推送；README §12/§14/§15 已按 A2a/B2/B1 素材填实，**仅 §13 待 dev-A A4 素材**）　｜　版本：v1.2　｜　日期：2026-11-13
> 负责人：**dev-C（唯一）**　｜　任务包：**C · 前端构建与文档齐套线**　｜　容量：上限 24h（3 人日）；实排开发 **15.5h ＝ 1.94 人日**，余额为 Line 1 联调槽位占用＋缓冲
> 关联：docs/team_tasks/day10/day10-任务总览与DAG.md v1.0（§2 DAG、§3 对接点 Y1/Y3、§4 写域）｜docs/plans/day10/day-10-交付与观测-工作推进计划.md v1.0 §4.3
> 上游依据：PRD §2.1（单仓库三入口）、§1.3（教学 MVP 口径）、§3.2（身份规则口径唯一来源）、line 578（文档清单）｜技术设计文档 §4/§6（十组件口径）｜大纲 §6.2（十组件表）、§11.3（交付文档清单）
> 角色口径说明：HARNESS_DESIGN / IDENTITY_RULES / AI_USAGE 三份文档是 TD/大纲明列的**开发实现口径交付物**（与本包承接的前端构建同域自产），README 收口是对既有开发成果的结构化沉淀——均非独立文档角色工作（沿 day09 总览同一口径）
> 变更记录见文末

---

# dev-C · Day 10 任务书 · 前端构建与文档齐套线

## 0. 你的边界（先读这条）

### 0.1 写域仅限（越界即返工）

```
frontend/frontend.Dockerfile          （新增；命名如 A 需调整在 M6.0 定）
README.md                             （增量，本包独占）
docs/HARNESS_DESIGN.md                （新增）
docs/IDENTITY_RULES.md                （新增）
docs/AI_USAGE.md                      （新增）
docs/SECURITY.md                      （复核修订，day09 A2 交付后的现状复核）
docs/plans/day10/**                   （计划件维护）
docs/team_tasks/day10/**              （总览与本文维护）
```

### 0.2 禁触（他人写域，需改动时提需求不直接改）

backend `Dockerfile`/`docker-compose.yml`/`deploy/nginx.conf`（dev-A 独占——**你只交 Y1 静态段草案，nginx 由 A 合入**）、观测代码与 `app/api/metrics.py`（B）、`tests/eval_gate.py`/`.githooks/`（D）、`app/**` 全部业务代码。

### 0.3 素材接收口（你独占 README，素材来自他人）

| 素材 | 提供方 | 合入章节 |
| --- | --- | --- |
| 回滚演练记录＋部署门禁清单 | A4 | 「回滚」章节 |
| 成本估算＋四类指标口径 | B2 | 「成本数据」章节 |
| 门禁复跑口径 | D1 | 「评测门禁」相关小节 |
| 干净主机演练缺漏回改 | D3（提单） | 部署章节修订 |

### 0.4 你的 Line 1 槽位（优先级高于本包）

D10 穿插 day09 C6 联调；D11 穿插 M5.4 验证。冲突时 Line 1 优先。

---

## 1. 节点 C1 · 前端镜像与三入口构建（4h）

**前置**：M6.0。**D10 第一个开工项。**
**对接**：**提供 Y3**（产物路径契约）给 A2/A3；产出 M6.1 判据之一。

### 要做什么

1. `frontend/frontend.Dockerfile` 多阶段：依赖安装 `pnpm@11.15.1 --frozen-lockfile`（**2026-11-13 主理人裁决修订**：目录内仅 pnpm-lock.yaml，`npm ci` 不可执行）→ `vite build` → nginx 托管（容器内全新安装，不受本机 Vite 缓存影响；**不要动本机 `node_modules/.vite/deps`，不要 `--force`**）。
2. Vite MPA 三入口 `pad/console/family` 产物分目录。
3. **红线：pad 的 three-vrm 不进 console/family 构建产物**（PRD §2.1 单仓库三入口的初衷）。
4. 产物路径按 Y3 契约（`/usr/share/nginx/html/{pad,console,family}`）交 A 合入 nginx。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | `frontend/frontend.Dockerfile`、构建说明 | 容器内构建成功 |
| 2 | 三入口产物 | `grep -r "three-vrm" dist/console dist/family` **零命中**；`dist/pad` 含 VRM 资产 |
| 3 | Y3 契约闭环 | 三入口 URL（`/pad` `/console` `/family`）经 nginx 可打开且静态资源 200 |

---

## 2. 节点 C2 · README 收口（3h，D12）

**前置**：C1；素材依赖 A4（回滚）、B2（成本）、D1（门禁）。
**对接**：产出 M6.3 判据之一；D3 的缺漏提单回改本章节。

### 要做什么（四个新章节＋索引更新）

1. 「Docker 部署」：`git clone && cp .env.example .env && docker compose up` 三步口径＋服务清单表＋端口表。
2. 「回滚」：合入 A4 演练记录与部署门禁清单。
3. 「成本数据」：合入 B2 输出——S1 闭环率 ≥60% 目标、单会话 Tool ≤8、cache 命中率、分层省 Token；**数字全部取实测，未达标如实写**。
4. 「观测说明」：指标端点、Trace 查询方法、告警规则说明（大纲 §11.3 验收项）。
5. 更新 §10 文档索引。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | `README.md` 增量 | 新章节四项齐全；所有数字与 `eval/reports/`、`/metrics` 实测一致（抽查 3 个数字可回溯） |
| 2 | 教学口径保留 | 「局限」节保留教学 MVP 口径（PRD §1.3：不以模拟数据比例作为真实运营效果证明） |

---

## 3. 节点 C3 · docs/HARNESS_DESIGN.md 新建（3h，可与 C1 并行）

**前置**：M6.0。**对接**：产出 M6.3 判据之一。

### 要做什么

按大纲 §6.2 十组件表逐组件写「本仓落点」——组件行为＋对应文件路径＋关键配置：Context / System One / Tool / State / Checkpoint / Memory / Loop / Policy / Recovery / Observability / Evaluation。**全部路径实测存在后才能写入（写前逐个 `ls`）**。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | `docs/HARNESS_DESIGN.md` | 十组件逐项覆盖；文中每个文件路径 `ls` 通过（**零虚构路径**） |
| 2 | 口径一致性 | 与 TD §4/§6 一致，冲突处以 TD 为准并回改本文档 |

---

## 4. 节点 C4 · docs/IDENTITY_RULES.md 新建（2.5h）

**前置**：M6.0。**对接**：产出 M6.3 判据之一。

### 要做什么（口径唯一来源＝PRD §3.2）

1. 四层身份锚点：设备绑定→房间默认→声纹→主动确认。
2. 三档级联：声纹失败→主动确认→通用模式。
3. 阈值口径 **0.62**，含沿革（0.85→0.55→0.62）与依据（`eval/reports/声纹调优报告.md`）。
4. 通用模式边界：不读记忆/不写健康/不确认用药。
5. `thread_id=(elder_id, session_seq)` 服务端生成。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | `docs/IDENTITY_RULES.md` | 阈值、候选集（设备 1v1、恒 1 位）、级联行为三处与 PRD v3.1 **逐字一致** |
| 2 | 行为可追溯 | 文中行为均有对应测试名可引（如 `tests/test_gates.py`、声纹相关用例） |

---

## 5. 节点 C5 · docs/AI_USAGE.md 新建 ＋ SECURITY 复核（3h）

**前置**：M6.0。**对接**：产出 M6.3 判据收尾。

### 要做什么

1. `AI_USAGE.md` 三节：AI 辅助范围（哪些模块 AI 生成）、人工审查记录（高风险五类文件 auth/迁移/Tool 权限/MCP/Dockerfile 的人工读 diff 记录）、关键决策（引用 day02–day10 计划件的裁决记录）。
2. `docs/SECURITY.md` 按 day09 收口后现状复核：威胁模型/降级矩阵/数据保留/HITL 清单四节齐全，与 `tests/test_gates.py`、`eval/fixtures/injection.jsonl` 实况对齐。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | `docs/AI_USAGE.md` | 三节齐全且引用真实计划件路径 |
| 2 | `docs/SECURITY.md` 复核修订 | 四节齐全；引用的测试文件与夹具 `ls` 存在；HITL 清单与 PRD（用药 HITL、健康黄/红复核）一致 |

---

## 6. 执行记录（开工后填写）

| 日期 | 节点 | 结果/证据指针 | 等待项 |
| --- | --- | --- | --- |
| 2026-11-13 | C1 ✅ | `frontend/frontend.Dockerfile` 交付（多阶段 pnpm@11.15.1 --frozen-lockfile，构建上下文=frontend/）；容器构建成功 489MB；三入口产物齐全；three-vrm 红线 grep 零命中加固验证；pad 含 14 个 .vrm；三入口独立冒烟 200；Y1/Y3 契约草案备妥 | M6.1 全栈 nginx 终验待 A3 合入 |
| 2026-11-13 | C3 ✅ | `docs/HARNESS_DESIGN.md` 交付；十组件覆盖；58 路径批量实测零缺失；4 处大纲 vs TD 差异注明以 TD 为准 | 无阻塞 |
| 2026-11-13 | C4 ✅ | `docs/IDENTITY_RULES.md` 交付；五节齐全；PRD §3.2 逐字机械比对通过；21 测试实存验证；发现大纲 §7.1 两处过时表述（0.85/候选列表）登记待勘误 | 大纲勘误另立 |
| 2026-11-13 | C5 ✅ | `docs/AI_USAGE.md` 交付（三节，56 引用路径全通过）；`docs/SECURITY.md` 复核至 v1.1（补降级矩阵/数据保留/HITL 三节） | auth 审查底稿待主理人补签；闸门②用例回填归 M5.1；后端 Dockerfile 人工读 diff 待 A1 |
| 2026-11-13 | C2 ✅（骨架） | `README.md` +112/−1：§12–§15 四新章节 + §10 索引；占位符 15 处显式标注，零编造数字 | 待 A4/B2/B1/D1 素材回填（D12）；待 Q1/Q3 裁决回填 |

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-13 | 首版，与总览 v1.0 同批产出。 |
| v1.1 | 2026-11-13 | C1 执行口径修订（主理人裁决）：依赖安装 `npm ci` → `pnpm@11.15.1 --frozen-lockfile`（目录内仅 pnpm-lock.yaml）；C1 已交付（`frontend/frontend.Dockerfile` 构建成功，三项自检全过），Y1/Y3 契约草案交 dev-A。 |
