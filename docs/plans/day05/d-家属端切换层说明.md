# Day 5 · dev-D 家属端 Mock→真实切换层说明

> 状态：部分完成（B-3 v1.1 已到，等待 X3 冻结）　｜　版本：v1.4　｜　日期：2026-11-07
> 关联：docs/plans/day05/d-双端前端推进计划.md（W2）、docs/plans/day05/d-DAG.md（N6/N7/N8）
> 上游依据：《接口契约_家属端数据》v1.1 草案 §4/§6（S-11）｜身份契约 v1.8（E3 `scope_elder_ids`、裁决 A-01）
> 变更记录见文末

---

## 1. 分层结构（N6 交付）

| 文件 | 只放什么 | 不放什么 |
| --- | --- | --- |
| `frontend/src/family/api.ts` | 认证域（`Principal` / `CurrentIdentity` / `login` / `fetchCurrent` / `logout` / `authedFetch` / refresh）、**唯一开关 `USE_MOCK`**、业务类型再导出、5 个业务函数签名与双轨分支 | 业务 Mock 数据、业务类型定义 |
| `frontend/src/family/data-source.ts` | 业务类型（`BoundElder` / `DailyReportSummary` / `DailyReportDetail` / `FamilyNotification` / `MedicationStats` / `AttentionEvent` / `Paged` / `ListItems`）、Mock 数据、Mock 实现、`SCOPE_FORBIDDEN_TEXT` | 认证态、localStorage、fetch |
| `frontend/src/family/pages/{HomePage,ReportsPage,NotificationsPage}.tsx` | 页面渲染 | **零改动**（import 路径与调用签名不变） |

页面调用链保持不变：`page → api.ts 业务函数 → (USE_MOCK ? data-source Mock : 真实 fetch)`。

## 2. 开关位置与默认态

- 唯一开关：`frontend/src/family/api.ts` → `export const USE_MOCK = true;`
- **默认态：`true`（Mock）**。后端 `/api/family/*` 5 端点属 Day 8 批次，本日未实现。
- 全仓 family 侧 `USE_MOCK` 只有 1 处定义；不读 `import.meta.env`、不读 `.env`。
- 依据：家属端契约 §6 **S-11**（自裁行：`api.ts` 内常量 + 内置数据，不进环境变量体系，避免被误配置成「永远 mock」）。
- K1 差异：`dev-d` 卡原文写「环境开关」，与 S-11 冲突；本轮按 S-11 执行，差异已登记 `d-家属端契约差异表.md` L6，回传 dev-B（N4）。**若要改环境变量，须契约负责人修订 S-11 + 冻结评审留痕后，与代码同一次提交，dev-D 不自行改。**

## 3. 双轨分支与契约对齐（N7 交付）

| 函数 | Mock 分支 | 真实分支（**待冻结**） | 契约出处 |
| --- | --- | --- | --- |
| `fetchBoundElders()` | `mockBoundElders()` | `GET /api/family/elders`，只读 `{items}` | §4 F0 |
| `fetchReports(elderId)` | `mockReports(elderId)` | `GET /api/family/elders/{elder_id}/reports?page=1&page_size=100` | §4 F1 |
| `fetchReport(reportId)` | `mockReportDetail(reportId)` | `GET /api/family/reports/{report_id}` | §4 F2 |
| `fetchNotifications()` | `mockNotifications()` | `GET /api/family/notifications?page=1&page_size=100&unread_only=false` | §4 N1 |
| `markNotificationRead(id)` | `mockMarkNotificationRead(id)` | `POST /api/family/notifications/{id}/read` | §4 N4 |

已落实的契约要点：

1. `medication_stats` 四键必全（`taken_reported` / `refused` / `missed_unconfirmed` / `notification_failed`），页面分列展示、不合并为「实际漏服」。
2. `audio_url` P0 恒 `null`，页面不渲染播放器（FR-RPT-04 P1）。
3. `payload` 未知键忽略不报错：页面只消费 `elder_name` / `event_brief` / `report_id`。
4. N4 幂等：重复调用返回原 `read_at`；只写 `read_at`，不动 `status`（S-03）。
5. 403 → `SCOPE_FORBIDDEN_TEXT`（`无权查看该长者的数据`），不回显 `trace_id`；401 → 统一登录失效处理（`console/api.ts` 同款口径）。
6. 长者切换器数据源只来自 F0（服务端已按 `guardian_id=:current AND status='active'` 过滤）；当前身份由 `GET /api/sessions/current`（E3，含 `scope_elder_ids`）在入口层恢复，**前端不做二次关系判定、不按 `elder_id` 自建绑定、不开长者档案接口**（裁决 A-01）。代码中 0 处硬编码 `elder_id` 作为关系判定依据；F0 的 `elder_id`/`name`/`relation` 原样用于展示与切换。

**浏览器回归（`USE_MOCK=true`，2026-11-07）**：家属账号登录后，长者切换器出现 2 位 Mock 长者；日报列表出现日期卡片，详情展示用药四类分列与固定声明；通知中心 3 条、未读 2 条，点开一条未读后未读变 1 条（同时按 `payload.report_id` 跳转日报）。页面 import 与调用签名未变，Mock 渲染与迁移前一致。

**375px 真实视口验证（2026-11-07，无头 Chrome CDP，视口 375×812）**：日报列表 / 通知中心 / 日报详情三页均 `window.innerWidth=375`、`documentElement.scrollWidth=375`、`body.scrollWidth=375`，逐元素扫描溢出元素 0 个；正文 16px、次级说明 14px、tab 与退出按钮高度 48px。截图存外部：`family-375-reports.png`、`family-375-notifications.png`、`family-375-detail.png`。**通过**。

**契约 v1.1 变化（2026-11-07，B-3 部分输入）**：R3 `elder_guardians` 字段表补 `id`/`assigned_at`，但 F0 端点仍只返回 `elder_id`/`name`/`relation`（裁剪不变，S-05）；S-01 的 `medication_stats` 四键改为 TD 596 原文，不再算自裁。两处均**不影响前端代码**（F0 不消费 id/assigned_at；四键原本就与 TD 原文一致）。5 端点路径/响应外壳未变，真实分支继续标注「待冻结」，等待 X3 冻结评审。

## 4. Day 8 后端交付后的切换步骤

1. 后端实现并注册 5 端点（F0/F1/F2/N1/N4），按契约 §4 返回外壳与字段。
2. 契约冻结评审（X3）通过、B-3 校对稿（X2）差异闭环；`d-家属端契约差异表.md` 逐端点标注结论。
3. 把 `frontend/src/family/api.ts` 的 `USE_MOCK` 改为 `false`（唯一改动点），`pnpm --dir frontend build`。
4. 本地起后端 + 前端 `pnpm --dir frontend dev`，逐页联调：
   - 长者切换器（F0）→ 日报列表（F1）→ 日报详情（F2）；
   - 通知列表（N1）→ 点卡片已读（N4，刷新后仍为已读）；
   - 换未绑定长者 / 他人 `report_id` 应得 403 → 页面显示 `SCOPE_FORBIDDEN_TEXT`，无白屏；
   - `audio_url` 恒 null 时不出现播放器；`payload` 增加未知键时页面不报错。
5. 联调通过后按 S-11：**删除 mock 分支 + 删除 `data-source.ts` 的 Mock 数据与实现**（业务类型保留在 `data-source.ts` 或并回 `api.ts`，按当次评审口径），**同一次提交登记修订记录**。

## 5. Mock 删除条件（S-11 原文口径）

- 触发条件：Day 8 后端 5 端点交付 + 家属端契约冻结 + 前端联调通过。
- 删除范围：`USE_MOCK` 常量、Mock 数据、Mock 实现函数。
- 必须同一次提交：契约修订记录 / 差异表收口 / 删除说明。
- 未满足上述条件前，**不得删除 Mock、不得把真实分支当已验收**。

## 6. 当前「待冻结」清单

| # | 事项 | 状态 |
| --- | --- | --- |
| 1 | F0/F1/F2/N1/N4 真实分支口径（路径已验证，字段与外壳待 B-3） | ⏳ X2 |
| 2 | `d-家属端契约差异表.md` 逐端点结论 | ⏳ X2 |
| 3 | 家属端契约冻结评审（A1） | ⏳ X3 |
| 4 | K1：环境开关 vs S-11 常量 | 已按 S-11 执行；等待 dev-B 回执 |
| 5 | 真实端点联调与 Mock 删除 | ⏳ Day 8 |

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | G0 建表占位 |
| v1.1 | 2026-11-07 | N6/N7 完成后填写：分层结构、开关位置与默认态、5 函数双轨、Day8 切换步骤、Mock 删除条件、待冻结清单。真实分支全部标注「待冻结」，未删 Mock |
| v1.2 | 2026-11-07 | 补 Mock 浏览器回归结果（切换器/日报列表+详情/通知未读 2→1）；真实 375px 视口未验证（工具限制），真实分支仍未定稿 |
| v1.3 | 2026-11-07 | 补 375px 真实视口验证（无头 Chrome CDP 375×812）：三页无横向滚动、溢出元素 0、正文 16px、触控 48px，通过；真实分支仍未定稿 |
| v1.4 | 2026-11-07 | merge origin/develop 后接收契约 v1.1：R3 补 id/assigned_at（F0 响应不变）、S-01 改 TD 原文；前端代码无需调整；5 端点路径/外壳未变，真实分支继续待冻结 |
