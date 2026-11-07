# Day 5 · dev-D（console / family）— 执行 DAG

> 状态：草稿　｜　版本：v1.0　｜　日期：2026-11-07
> 派生自：`docs/plans/day05/d-双端前端推进计划.md` v1.0（§3 W0–W5、§4 边界、§6 自检）
> 关联卡：`docs/team_tasks/day05/dev-d-前端console与family.md`　｜　上游分工图：`docs/team_tasks/day05/dag-项目工作流总览与联调节点.md`（N3 / N4 / M5.4）
> **本图是派生物，不承载口径。** 与主计划冲突时以主计划为准；主计划改了这张图必须同提交同步。工时数字仅用于排并行波次，不是交付承诺。

---

## 1. 图（Mermaid）

```mermaid
flowchart TB
    G0["G0 · 30m<br/>启动门槛<br/>三待确认 + 基线构建 + 写锁通告"]

    subgraph SD1["D-1 线 · admin 管理页（无前置，M5.4 硬依赖）"]
        N1["N1 · 45m<br/>admin-api.ts<br/>E12/E15/E13 封装"]
        N2["N2 · 60m<br/>AdminUsersPage<br/>建员工/建家属/绑定"]
        N3["N3 · 60m<br/>AdminDevicesPage<br/>E18/E16/E17 + 一次性密钥"]
        N4["N4 · 40m<br/>App.tsx + AppLayout + index.css<br/>【写锁 L1】"]
        N5["N5 · 50m<br/>四流程走查录屏<br/>+ 非 admin 403 反向验证"]
        N1 --> N2
        N1 --> N3
        N2 --> N4
        N3 --> N4
        N4 --> N5
    end

    subgraph SD2["D-2 线 · family 切换层（被 N4 联调阻塞）"]
        N6["N6 · 45m<br/>数据源抽象骨架<br/>类型/Mock 迁出 api.ts"]
        N7["N7 · 60m<br/>双轨切换层<br/>5 端点真实分支"]
        N8["N8 · 40m<br/>N4 校对稿 → 差异表<br/>+ 切换层说明（待冻结）"]
        N6 --> N7 --> N8
    end

    subgraph SD3["D-3 线 · console 五 Mock 页（被 N3 联调阻塞）"]
        N9["N9 · 45m<br/>现状字段盘点<br/>五页类型全导出"]
        N10["N10 · 50m<br/>A-4 草案 → 字段差异清单<br/>双向确认（N3）"]
        N11["N11 · 50m<br/>五 Mock 页字段修正<br/>+ 渲染回归"]
        N9 --> N10 --> N11
    end

    N12["N12 · 40m<br/>联调与构建门禁<br/>build + 三项静态零 + N3/N4 检查单"]
    N13["N13 · 40m<br/>M5.4 演示预演<br/>卡状态回填 + 汇报清单"]

    X0(["X0 主理人<br/>A-4 契约草案落点裁定 #6"])
    X1(["X1 dev-A<br/>A-4 契约草案 · N3"])
    X2(["X2 dev-B<br/>B-3 校对稿 · N4"])
    X3(["X3 主理人<br/>家属端契约冻结评审 A1"])
    X4(["X4 主理人<br/>M5.4 演示脚本"])
    X5(["X5 基线<br/>pnpm build 现状"])

    X5 -.基线结果.-> G0
    X0 -.落点.-> X1
    G0 --> N1
    G0 --> N6
    G0 --> N9
    X1 -.硬输入.-> N10
    X2 -.硬输入.-> N8
    X3 -.冻结前不定稿.-> N8
    N5 --> N12
    N8 --> N12
    N11 --> N12
    N12 --> N13
    X4 -.脚本.-> N13
    N5 -.D-1 是 M5.4 硬依赖.-> X4

    classDef crit fill:#ffe6cc,stroke:#d79b00,color:#000
    classDef side fill:#dae8fc,stroke:#6c8ebf,color:#000
    classDef wait fill:#f5f5f5,stroke:#888,color:#000
    classDef ext fill:#e1d5e7,stroke:#674b7b,color:#000
    class G0,N1,N2,N3,N4,N5,N12,N13 crit
    class N6,N7,N9 side
    class N8,N10,N11 wait
    class X0,X1,X2,X3,X4,X5 ext
```

图例：**橙 = 关键路径（admin 线，305 min）**；蓝 = 可先行的骨架/盘点旁支；灰 = 被外部输入闸门阻塞的收口节点；紫 = 外部节点（非 dev-D 产出）。

## 2. ASCII 兜底（渲染失败时看这个）

```
                         X0 落点裁定 ──> X1 dev-A A-4 草案 ──┐
                         X5 基线 build ─┐                    │
                                       ↓                    │
                                  G0 启动门槛(30m)           │
                    ┌──────────────────┼─────────────────────┼──────────────┐
                    ↓                  ↓                     ↓              │
   ★ D-1 线        N1 45m          N6 45m 骨架           N9 45m 盘点      │
   （关键路径）       │                 │                     │              │
                 ┌───┴───┐             ↓                     │              │
                 ↓       ↓           N7 60m 双轨             │              │
               N2 60m  N3 60m          │                     │              │
                 └───┬───┘             │                     │              │
                     ↓                 │                     │              │
                   N4 40m【写锁 L1】    │                     │              │
                     ↓                 │                     │              │
                   N5 50m 走查录屏      │                     │              │
                     │                 ↓                     ↓              │
                     │           X2 dev-B B-3 ──> N8 40m  X1 ──> N10 50m   │
                     │           X3 冻结评审 ─┘            N9 ──┘    │      │
                     │                 │                            ↓      │
                     │                 │                         N11 50m    │
                     │                 │                            │      │
                     └─────────────────┴────────────────────────────┼──────┘
                                       ↓                            ↓
                                   N12 40m 联调与构建门禁 <─────────┘
                                       ↓
                              X4 演示脚本 ──> N13 40m M5.4 预演 + 卡回填
                              N5 ──(D-1 硬依赖)──> X4

关键路径：G0 → N1 → max(N2,N3) → N4 → N5 → N12 → N13
         = 30 + 45 + 60 + 40 + 50 + 40 + 40 = 305 min（约 5h05m）
旁支 D-2：G0 → N6 → N7 → N8 = 175 min，被 X2/X3 阻塞在 N8
旁支 D-3：G0 → N9 → N10 → N11 = 185 min，被 X0/X1 阻塞在 N10
串行基线 = 655 min；三线并行后墙钟 ≈ 305 min（节省约 53%）
实线 = 硬依赖（前者产物是后者输入）；虚线 = 外部输入 / 软约束
```

## 3. 节点表（写作用域即禁触边界）

| 节点 | 对应主计划 | 产出文件（写作用域） | 依赖 | 外部输入 | 预估 |
| --- | --- | --- | --- | --- | --- |
| **G0** | W0 | `d-启动门槛核对.md` + 三份空登记表（`docs/plans/day05/d-*.md`） | X5 | X0/X2/X3 时点答复 | 30m |
| **N1** | W1-a | `frontend/src/console/admin-api.ts`（新建） | G0 | 身份契约 §4 E12/E15/E13（已冻结） | 45m |
| **N2** | W1-b | `console/pages/AdminUsersPage.tsx`（新建） | N1 | — | 60m |
| **N3** | W1-b | `console/pages/AdminDevicesPage.tsx`（新建）；**复用** `elders-api.ts` 不改签名 | N1 | E18/E16/E17 现有函数 | 60m |
| **N4** | W1-c | `console/App.tsx`、`console/layout/AppLayout.tsx`、`console/index.css`（**L1**） | N2、N3 | — | 40m |
| **N5** | W1-d/e | `d-admin页走查记录.md` + 录屏（外存，只登记路径） | N4 | K2 决策（推荐 B） | 50m |
| **N6** | W2-a | `family/data-source.ts`（新建）+ `family/api.ts` 拆分 | G0 | 契约 §0.4 第 2 条 | 45m |
| **N7** | W2-b/c | `family/api.ts`、`family/pages/{HomePage,ReportsPage,NotificationsPage}.tsx` | N6 | 契约 §4 五端点（草案） | 60m |
| **N8** | W2-d/e | `d-家属端契约差异表.md`、`d-家属端切换层说明.md` | N7、X2 | X3（软：冻结前不定稿） | 40m |
| **N9** | W3-a | `d-字段差异清单.md` 左半表 | G0 | TD §3 表定义 | 45m |
| **N10** | W3-b/c | `d-字段差异清单.md` 全表 + 回传留痕 | N9、X1 | X0（落点） | 50m |
| **N11** | W3-d/e | 五个 `console/*-api.ts` 及必要 `pages/*.tsx`（**L3**） | N10（须双向确认） | — | 50m |
| **N12** | W4 | `d-汇报清单.md` + 构建输出摘要 | N5、N8、N11 | N3/N4 检查单 | 40m |
| **N13** | W5 | 任务卡变更记录 + `> 状态：` 行；计划件与索引状态 | N12、X4 | M5.4 演示脚本 | 40m |
| X0–X5 | — | **非 dev-D 产出** | — | 主理人 / dev-A / dev-B / 基线 | — |

## 4. 边表（谁把什么交给谁）

| # | 从 | 到 | 类型 | 传递物 | 断了会怎样 |
| --- | --- | --- | --- | --- | --- |
| 1 | X5 | G0 | 软 | 当前 `pnpm --dir frontend build` 结果（不回归对照基线） | 无法区分「本次改动引入的错误」与既有错误 |
| 2 | G0 | N1 / N6 / N9 | 硬 | 三待确认结论、写作用域冻结、三条并行线放行 | 三线抢同一批共享文件，或对准未定的契约落点开发 |
| 3 | N1 | N2、N3 | 硬 | `createStaff / createGuardian / bindGuardian` 函数签名与 403/409/422 分支 | 两页各自造一套请求封装 → 违反禁区 8 |
| 4 | N2、N3 | N4 | 硬 | 两个页面组件 + admin 菜单 key | admin 登录后仍落 `PlaceholderHome`，四流程不可达 |
| 5 | N4 | N5 | 硬 | 可操作的 admin 分流 + 新增样式 | 走查无对象，M5.4 缺前端环节 |
| 6 | N6 | N7 | 硬 | 业务类型 / Mock 数据独立模块 + 唯一 `USE_MOCK` | 双轨无处挂载，切开关要改页面 import（违反契约 §0.4） |
| 7 | X2、N7 | N8 | 硬 | B-3 校对稿 + 已成型双轨 | 差异表无输入；真实分支口径无依据 |
| 8 | X3 | N8 | 软 | 冻结评审结论 | 允许先交「待冻结」版；**擅自定稿即违规** |
| 9 | X0、N9、X1 | N10 | 硬 | 落点 + 现状字段盘点 + A-4 草案 | 差异清单无出处，出现凭空字段 |
| 10 | N10 | N11 | 硬 | dev-A **双向确认**结论 | 单方定稿，Day7/8 建表时口径漂移 |
| 11 | N5、N8、N11 | N12 | 硬 | 三线全部产出 + 录屏/差异表/清单 | 构建门禁覆盖不全，汇报清单漏项 |
| 12 | N12、X4 | N13 | 硬 | 门禁通过证据 + 演示脚本 | M5.4 预演无依据，卡无法回填 |
| 13 | N5 | X4 | 软 | admin 段走查结论（`D1 --> M54`） | 演示脚本须临时去掉 admin 段并在卡内写明偏差（R9） |
| 14 | X1 | N3 联调节点 | 软 | 差异清单回传 dev-A（**dev-D → dev-A** 反向边） | dev-A 草案得不到前端现状反馈，N3 只完成一半 |

> 「硬」= 没有它下一步无法产出正确结果；「软」= 缺了能降级继续，但须登记偏差。

## 5. 并行编排（实际怎么派）

| 波次 | 并行度 | 干什么 | 前置闸门 |
| --- | --- | --- | --- |
| **W1** | 1 | G0：三待确认 + 基线 build + 写锁通告 | X5 |
| **W2** | **3** | ① **N1**（admin 封装，关键路径起步）② **N6**（family 骨架，不依赖 B-3）③ **N9**（五页盘点，不依赖 A-4） | G0 全绿；三线写作用域完全不相交 |
| **W3** | **3** | ① N1 交付后并行 **N2 + N3** ② **N7** ③ 等 X1/X2 期间做 N10 的模板与 N8 的空表 | N1、N6 |
| **W4** | 2 | ① **N4**（L1 写锁，合并 N2/N3）② X1 到手 → **N10**；X2 到手 → **N8** | N2、N3、N7、X1、X2 |
| **W5** | 3 | ① **N5** 走查录屏 ② **N11** 字段修正（须双向确认）③ N8 收口 | N4、N10 |
| **W6** | 1 | **N12** 联调与构建门禁（三线汇合，唯一构建窗口） | N5、N8、N11 |
| **W7** | 1 | **N13** M5.4 预演 + 卡回填 + 索引状态更新 | N12、X4 |

**关键路径 305 min** 由 D-1 线决定；D-2 / D-3 两条旁支分别 175 / 185 min，落在 W2–W5 窗口内，**前提是 X1（A-4）与 X2（B-3）在 W4 之前到手**。若 X1 或 X2 迟到，对应收口节点（N10 / N8）挂起并登记阻塞，**不猜测口径、不阻塞 D-1 关键路径**。

## 6. 写锁（违反即返工）

| 锁 | 文件 | 唯一写者 | 说明 |
| --- | --- | --- | --- |
| **L1** | `console/App.tsx`、`console/layout/AppLayout.tsx`、`console/index.css` | **N4** | N11 若需同步改 `index.css` 类名，**并入 N4 一次改完**，不与 W3 并行 |
| **L2** | `family/api.ts` 及其 `pages/*` | N6 → N7 → N8 同一人串行 | 与 console 线完全隔离；禁止 import console / pad |
| **L3** | 五个 `console/*-api.ts` + 对应 pages | **N11** | 不改 `elders-api.ts`（只读复用）、不改 `console/api.ts`（K2-B） |
| **L4** | `src/theme/tokens.css`、`vite.config.ts`、`package.json`、`*.html` | **本轮无人** | 三端共享；只引用已有变量；确需改动先通告 dev-C 并三端回归（R6） |
| **L5** | `src/pad/**` | dev-C | dev-D 禁触 |
| **L6** | `docs/接口契约_*.md`、三份基线文档正文 | 契约负责人 / 主理人 | dev-D 只登记差异，不改正文；修订记录须同提交 |
| **L7** | `docs/team_tasks/day05/PROGRESS.md` | `scripts/update_progress.py` | 自动生成，**禁止手改**；要动进度只改任务卡 `> 状态：` 行 |
| **L8** | `docs/plans/day05/d-*.md` | dev-D | 本 DAG 与主计划及其派生件 |

## 7. 每道门禁的验收门（节点交付必须附证据）

| 门 | 覆盖节点 | 必须自证（对应主计划 §6） | 量化门槛 |
| --- | --- | --- | --- |
| **G-A** | G0 | 三待确认有结论或显式 ⏳；基线 build 输出；写锁通告留痕；**代码改动 = 0** | 3/3；0 处代码改动 |
| **G-B** | N1–N4 | admin 两项菜单 enabled；四流程可操作；`elders-api.ts` 签名零改动；新增 CSS 零硬编码 hex、零未定义变量 | 4 项菜单/流程全通；0 处越界 |
| **G-C** | N5 | 四流程走查录屏 + 非 admin 三角色 403 反向验证；一次性密钥刷新后不再出现 | 4/4 流程；3/3 越权；密钥 1 次 |
| **G-D** | N6–N8 | `USE_MOCK` 定义 = 1 处；切 true/false 三业务页渲染一致；5 端点形状对齐契约 §4；契约正文改动 = 0 | 5/5 端点；0 处正文改动 |
| **G-E** | N9–N11 | 字段出处覆盖 100%；凭空字段 = 0；dev-A 双向确认留痕 ≥1 份；五页渲染无回归 5/5 | 100%；5/5 |
| **G-F** | N12 | `pnpm --dir frontend build` 通过；硬编码 hex / 未定义变量 / three-vrm 进 console 产物 三项均 = 0；N3、N4 检查单逐条有结论 | build 通过；3 项 = 0 |
| **G-G** | N13 | M5.4 admin 段一次通过；任务卡变更记录与 `> 状态：` 已回填；PROGRESS 未手改；索引同提交登记 | 全绿 |

## 8. 失败回退

| # | 触发 | 回退动作 | 不得做 |
| --- | --- | --- | --- |
| F1 | X0（A-4 落点）未定 | D-3 停在 N9；N10 挂起，差异清单先出空表 | 不凭经验自造字段、不新建第二套契约文件 |
| F2 | X1（A-4 草案）Day5 内未到 | N11 不启动；只交现状盘点 + 阻塞登记 | 不改五 Mock 页字段 |
| F3 | X2（B-3 校对稿）未到 | N8 挂起；N6/N7 骨架照交，真实分支标注「待冻结」 | 不擅自定稿、不改契约正文 |
| F4 | X3（冻结评审）未开 | D-2 交付「待冻结版」切换层，切换说明显式列出未冻结项 | 不把草案当冻结口径写进代码注释 |
| F5 | N5 走查未过（R9） | 立即上报主理人；M5.4 脚本可临时去掉 admin 段，卡变更记录写明偏差 | 不静默跳过演示项、不谎报走查通过 |
| F6 | K1 被裁定改环境变量 | 契约负责人改 S-11 + 修订记录，与 N6/N7 代码**同一次提交** | 不在代码里单方面引 `.env` |
| F7 | 构建门禁 G-F 失败 | 回到对应线修；D-1 关键路径优先修，旁支问题登记不阻塞 M5.4 | 不带病进 N13 / M5.4 |
| F8 | 共享文件冲突（L1/L4） | 停手，由唯一写者合并一次改完；涉及 tokens.css 先通告 dev-C | 不并行改同一文件 |

## 9. 执行纪律

1. **本轮只交付文档**：本 DAG 与主计划是计划件；不编写业务代码、不修改任何现有代码文件。
2. 后续执行阶段，每个节点完成即回报：`改了哪些文件（diff 摘要） → 自检结果（§7 对应门） → 遗留问题（责任人/时点）`；每道门全绿才进下一波次。
3. 外部节点 X0–X4 不属 dev-D，只登记依赖与阻塞，**不代主理人 / dev-A / dev-B 做裁定**。
4. 子任务不得改白名单外文件；发现必须改，先停手回报。
5. 大纲只提供流程；任何表名、字段、接口、阈值、UI 口径一律回 PRD 与技术设计文档。
6. 本图与主计划同一次提交登记进 `docs/plans/README.md`；主计划变更必须同步本图（AGENTS §10.3）。

## 10. 机器可读 JSON

```json
{
  "plan": "docs/plans/day05/d-双端前端推进计划.md",
  "role": "dev-D",
  "day": 5,
  "version": "v1.0",
  "units": "minutes (estimate only)",
  "critical_path": ["G0", "N1", "N2|N3", "N4", "N5", "N12", "N13"],
  "critical_path_minutes": 305,
  "serial_baseline_minutes": 655,
  "nodes": [
    {"id": "G0", "name": "启动门槛", "plan_ref": "W0", "minutes": 30, "gate": "G-A", "writes": ["docs/plans/day05/d-启动门槛核对.md"]},
    {"id": "N1", "name": "admin-api 封装", "plan_ref": "W1-a", "minutes": 45, "gate": "G-B", "writes": ["frontend/src/console/admin-api.ts"]},
    {"id": "N2", "name": "AdminUsersPage", "plan_ref": "W1-b", "minutes": 60, "gate": "G-B", "writes": ["frontend/src/console/pages/AdminUsersPage.tsx"]},
    {"id": "N3", "name": "AdminDevicesPage", "plan_ref": "W1-b", "minutes": 60, "gate": "G-B", "writes": ["frontend/src/console/pages/AdminDevicesPage.tsx"]},
    {"id": "N4", "name": "admin 分流装配", "plan_ref": "W1-c", "minutes": 40, "gate": "G-B", "lock": "L1", "writes": ["frontend/src/console/App.tsx", "frontend/src/console/layout/AppLayout.tsx", "frontend/src/console/index.css"]},
    {"id": "N5", "name": "四流程走查与越权反向验证", "plan_ref": "W1-d/e", "minutes": 50, "gate": "G-C", "writes": ["docs/plans/day05/d-admin页走查记录.md"]},
    {"id": "N6", "name": "family 数据源抽象骨架", "plan_ref": "W2-a", "minutes": 45, "gate": "G-D", "lock": "L2", "writes": ["frontend/src/family/data-source.ts", "frontend/src/family/api.ts"]},
    {"id": "N7", "name": "family 双轨切换层", "plan_ref": "W2-b/c", "minutes": 60, "gate": "G-D", "lock": "L2", "writes": ["frontend/src/family/api.ts", "frontend/src/family/pages/"]},
    {"id": "N8", "name": "N4 校对稿差异表与切换说明", "plan_ref": "W2-d/e", "minutes": 40, "gate": "G-D", "blocked_by": ["X2"], "writes": ["docs/plans/day05/d-家属端契约差异表.md", "docs/plans/day05/d-家属端切换层说明.md"]},
    {"id": "N9", "name": "五 Mock 页字段盘点", "plan_ref": "W3-a", "minutes": 45, "gate": "G-E", "writes": ["docs/plans/day05/d-字段差异清单.md"]},
    {"id": "N10", "name": "A-4 草案字段差异清单与双向确认", "plan_ref": "W3-b/c", "minutes": 50, "gate": "G-E", "blocked_by": ["X0", "X1"], "writes": ["docs/plans/day05/d-字段差异清单.md"]},
    {"id": "N11", "name": "五 Mock 页字段修正与渲染回归", "plan_ref": "W3-d/e", "minutes": 50, "gate": "G-E", "lock": "L3", "writes": ["frontend/src/console/*-api.ts", "frontend/src/console/pages/"]},
    {"id": "N12", "name": "联调与构建门禁", "plan_ref": "W4", "minutes": 40, "gate": "G-F", "writes": ["docs/plans/day05/d-汇报清单.md"]},
    {"id": "N13", "name": "M5.4 预演与卡状态回填", "plan_ref": "W5", "minutes": 40, "gate": "G-G", "writes": ["docs/team_tasks/day05/dev-d-前端console与family.md", "docs/plans/README.md"]}
  ],
  "external_nodes": [
    {"id": "X0", "name": "A-4 契约草案落点裁定（主理人 #6）", "owner": "主理人"},
    {"id": "X1", "name": "dev-A A-4 契约草案（N3）", "owner": "dev-A"},
    {"id": "X2", "name": "dev-B B-3 校对稿（N4）", "owner": "dev-B"},
    {"id": "X3", "name": "家属端契约冻结评审 A1", "owner": "主理人"},
    {"id": "X4", "name": "M5.4 演示脚本", "owner": "主理人"},
    {"id": "X5", "name": "基线 pnpm build 结果", "owner": "仓库现状"}
  ],
  "edges": [
    {"from": "X5", "to": "G0", "type": "soft", "payload": "基线构建结果"},
    {"from": "X0", "to": "X1", "type": "soft", "payload": "契约草案落点"},
    {"from": "G0", "to": "N1", "type": "hard", "payload": "三线放行与写作用域冻结"},
    {"from": "G0", "to": "N6", "type": "hard", "payload": "三线放行与写作用域冻结"},
    {"from": "G0", "to": "N9", "type": "hard", "payload": "三线放行与写作用域冻结"},
    {"from": "N1", "to": "N2", "type": "hard", "payload": "E12/E15/E13 函数签名"},
    {"from": "N1", "to": "N3", "type": "hard", "payload": "E12/E15/E13 函数签名"},
    {"from": "N2", "to": "N4", "type": "hard", "payload": "AdminUsersPage 组件与菜单 key"},
    {"from": "N3", "to": "N4", "type": "hard", "payload": "AdminDevicesPage 组件与菜单 key"},
    {"from": "N4", "to": "N5", "type": "hard", "payload": "可操作的 admin 分流"},
    {"from": "N6", "to": "N7", "type": "hard", "payload": "类型/Mock 独立模块与唯一 USE_MOCK"},
    {"from": "N7", "to": "N8", "type": "hard", "payload": "成型双轨"},
    {"from": "X2", "to": "N8", "type": "hard", "payload": "B-3 校对稿"},
    {"from": "X3", "to": "N8", "type": "soft", "payload": "冻结前不定稿"},
    {"from": "X0", "to": "N10", "type": "hard", "payload": "契约草案落点"},
    {"from": "X1", "to": "N10", "type": "hard", "payload": "A-4 草案"},
    {"from": "N9", "to": "N10", "type": "hard", "payload": "现状字段盘点"},
    {"from": "N10", "to": "N11", "type": "hard", "payload": "dev-A 双向确认结论"},
    {"from": "N5", "to": "N12", "type": "hard", "payload": "走查录屏与反向验证"},
    {"from": "N8", "to": "N12", "type": "hard", "payload": "差异表与切换说明"},
    {"from": "N11", "to": "N12", "type": "hard", "payload": "字段修正与渲染回归"},
    {"from": "N12", "to": "N13", "type": "hard", "payload": "门禁通过证据"},
    {"from": "X4", "to": "N13", "type": "hard", "payload": "演示脚本"},
    {"from": "N5", "to": "X4", "type": "soft", "payload": "D-1 是 M5.4 硬依赖"},
    {"from": "N10", "to": "X1", "type": "soft", "payload": "差异清单回传 dev-A（反向）"}
  ],
  "waves": [
    {"wave": "W1", "parallel": 1, "nodes": ["G0"]},
    {"wave": "W2", "parallel": 3, "nodes": ["N1", "N6", "N9"]},
    {"wave": "W3", "parallel": 3, "nodes": ["N2", "N3", "N7"]},
    {"wave": "W4", "parallel": 3, "nodes": ["N4", "N10", "N8"]},
    {"wave": "W5", "parallel": 3, "nodes": ["N5", "N11"]},
    {"wave": "W6", "parallel": 1, "nodes": ["N12"]},
    {"wave": "W7", "parallel": 1, "nodes": ["N13"]}
  ],
  "locks": {
    "L1": {"files": ["console/App.tsx", "console/layout/AppLayout.tsx", "console/index.css"], "owner": "N4"},
    "L2": {"files": ["family/api.ts", "family/pages/*"], "owner": "N6->N7->N8 串行"},
    "L3": {"files": ["console/*-api.ts"], "owner": "N11"},
    "L4": {"files": ["src/theme/tokens.css", "vite.config.ts", "package.json", "*.html"], "owner": "本轮无人"},
    "L5": {"files": ["src/pad/**"], "owner": "dev-C"},
    "L6": {"files": ["docs/接口契约_*.md", "三份基线文档正文"], "owner": "契约负责人/主理人"},
    "L7": {"files": ["docs/team_tasks/day05/PROGRESS.md"], "owner": "update_progress.py"},
    "L8": {"files": ["docs/plans/day05/d-*.md"], "owner": "dev-D"}
  }
}
```

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 首版：按 `d-双端前端推进计划.md` v1.0 的 W0–W5 与 §4 边界绘制。14 个执行节点 + 6 个外部节点 + 25 条依赖边；关键路径 305 min（D-1 线，M5.4 硬依赖）、串行基线 655 min；7 波次并行编排、L1–L8 八条写锁、G-A–G-G 七道门禁、F1–F8 八条失败回退、机器可读 JSON。标注 X1/X2 外部输入阻塞与「冻结前不定稿」约束 |
