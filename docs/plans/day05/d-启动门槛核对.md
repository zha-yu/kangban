# Day 5 · dev-D 启动门槛核对（G0 / W0）

> 状态：进行中　｜　版本：v1.2　｜　日期：2026-11-07
> 关联：docs/plans/day05/d-双端前端推进计划.md（W0）、docs/plans/day05/d-DAG.md（G0 / G-A）
> 上游依据：身份契约 v1.8（E12/E15/E13/E18/E16/E17）｜《接口契约_家属端数据》v1.1 草案（S-11）
> 变更记录见文末

---

## 1. 三个待确认问题（G0 必填）

| # | 问题 | 当前核查结果 | 状态 | 阻塞哪一步 | 谁跟进 |
| --- | --- | --- | --- | --- | --- |
| Q1 | A-4 契约草案落点：新建 `docs/接口契约_照护台业务数据.md` vs 并入现有契约文件（主计划 §6 #6 未闭环） | 工作区 `docs/接口契约_*.md` 仅两份（身份与权限、家属端数据），无照护台业务数据契约；`dev-a` 卡 A-4 仍为「未开始」，草案实体不存在 | ⏳ 未闭环 | D-3 的 N10（字段定稿）与 N11（Mock 修正） | 主理人裁定落点；dev-A 交付 A-4 |
| Q2 | 《接口契约_家属端数据》冻结评审（A1）时点 | 文档头仍为「草案（v1.1，2026-11-07，待契约冻结评审）」；仓库内无评审排期或结论 | ⏳ 未闭环 | D-2 的 N8（真实分支定稿、删除「待冻结」标注） | 主理人组织评审 |
| Q3 | B-3 家属端契约技术校对稿交付时点 | merge origin/develop 后确认：dev-B 已将契约修订到 **v1.1**（R3 补 `id`/`assigned_at`、S-01 改 TD 原文），`dev-b` 卡 B-3 标「已完成」；独立逐端点校对意见稿文件未在仓库中 | 部分闭环 | N8 逐端点比对与真实分支定稿仍待 X3 | dev-B 补校对意见稿（如有）；主理人排冻结评审 |

> 处置口径（按 DAG F1/F3）：未答复即显式 ⏳，只完成不依赖口径的骨架与盘点；不凭经验自造字段，不用 Mock 冒充真实端点验收。

## 2. 基线构建（X5 → G0，不回归对照）

执行时间：2026-11-07；命令：`pnpm --dir frontend build`（`tsc --noEmit && vite build`）。

结果：**通过**（exit 0，`✓ built in 2.78s`）。原始输出摘要：

```
dist/index.html                  0.48 kB │ gzip: 0.32 kB
dist/family.html                 0.49 kB │ gzip: 0.32 kB
dist/console.html                0.49 kB │ gzip: 0.32 kB
dist/assets/pad-B1tAearO.css    16.06 kB │ gzip: 4.11 kB
dist/assets/family-CT8A5jsK.css 23.53 kB │ gzip: 5.54 kB
dist/assets/console-Df0myOCy.css 29.20 kB │ gzip: 6.45 kB
dist/assets/family-D8W8cLiE.js  17.89 kB │ gzip: 5.94 kB
dist/assets/console-DqXa1aj6.js 78.26 kB │ gzip: 20.84 kB
dist/assets/client-BaUToD7D.js 222.44 kB │ gzip: 69.27 kB
dist/assets/pad-DhIFwqph.js    816.32 kB │ gzip: 210.95 kB
✓ built in 2.78s
```

提示（非错误）：`pad` chunk >500 kB 的既有告警；本任务不处理。

## 3. 写作用域与禁触通告（留痕）

| 对端 | 通告内容 | 状态 |
| --- | --- | --- |
| dev-C | `frontend/src/theme/tokens.css`、`vite.config.ts`、`package.json`、`frontend/*.html` 本轮 dev-D 不改；`src/pad/**` 不触碰 | 已在本文件登记；**无跨人即时通讯通道，未实际发送** |
| dev-A | N3 需要 A-4 草案：每字段标注 TD §3 表名 + 字段出处；dev-D 回传字段差异清单 | 已在本文件登记；未实际发送 |
| dev-B | N4 需要 B-3 校对稿：5 端点逐条结论；K1 差异（环境开关 vs S-11）回传 | 已在本文件登记；未实际发送 |

## 4. 本次执行选定的两项处置

| 编号 | 处置 | 依据 |
| --- | --- | --- |
| K2（403 处理） | 选 **方案 B**：在 `console/admin-api.ts` 内解析 403 状态码并抛页面级错误文案；`console/api.ts` 零改动 | 主计划 W1-d 推荐 B；影响面最小，不回归登录页错误分支；不与方案 A 并存 |
| K1（Mock 开关） | 维持 **`family/api.ts` 内唯一 `USE_MOCK` 常量**（现状 `true`），不引 `import.meta.env` / `.env` | 家属端契约 §6 S-11（自裁行，改环境变量须契约负责人修订 + 冻结评审） |

## 5. 环境核查（G0 时点 + 后续复测）

| 项 | G0 时点（16:00 前） | 后续复测（同日） |
| --- | --- | --- |
| 后端 8000 `/health` | 未运行（连接失败） | **200**，`X-Trace-Id` 存在 |
| PostgreSQL 5432 | 无监听 | **healthy**（`silvermate-postgres`） |
| `docker` daemon | 不可用（`dockerDesktopLinuxEngine` 管道不存在） | **就绪**（用户目录 Docker Desktop 启动后） |
| 本地 `psql` / `initdb` / `pg_ctl` | 未安装 | 未安装（改用 `docker exec psql`） |
| `pnpm` / `node` | 11.15.1 / v24.18.0，可用 | 同左 |

结论：G0 时点无法走查，故当时将 N5 标 blocked；随后启动 Docker Desktop、postgres/redis 容器与 uvicorn 后解除，N5 已完成（结果见 `d-admin页走查记录.md` v1.1）。数据库只连本地 `docker compose` 实例，未连生产。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 首版：登记 Q1–Q3 三项 ⏳（A-4 落点 / 家属契约冻结 / B-3 时点）、基线构建输出、写作用域通告留痕、K2-B 与 K1 处置、环境核查结论。本阶段代码改动为 0 |
| v1.1 | 2026-11-07 | 复测环境：Docker Desktop 启动后 postgres/redis healthy、`alembic upgrade head` 成功、uvicorn `/health` 200 → N5 解除并完成真实走查；Q1–Q3 仍 ⏳ 未变 |
| v1.2 | 2026-11-07 | merge origin/develop 后更新 Q3：B-3 契约已修订到 v1.1、dev-b 卡标已完成（独立校对意见稿未见），改为「部分闭环」；Q1/Q2 仍 ⏳ |
