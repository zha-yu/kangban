# Day 5 · dev-D 家属端契约差异表（N4 校对稿比对）

> 状态：进行中（本地逐端点自检完成；B-3 v1.1 已到，X3 冻结未排期）　｜　版本：v1.4　｜　日期：2026-11-08
> 关联：docs/plans/day05/d-双端前端推进计划.md（W2-d）、docs/plans/day05/d-DAG.md（N8）
> 上游依据：《接口契约_家属端数据》v1.1 草案 §2/§4/§6（待冻结）｜dev-B B-3 契约修订（2026-11-07）
> 变更记录见文末

---

## 0. 状态

- X2（B-3 校对稿）：**部分闭环**——dev-B 已将契约修订到 **v1.1**（R3 补 `id`/`assigned_at`、S-01 自裁理由纠正），`dev-b` 卡 B-3 标「已完成」；独立的逐端点校对意见稿文件未在仓库中，故本表逐端点结论仍待完整校对稿。
- X3（契约冻结评审）：⏳ 未排期；真实分支继续标「待冻结」。
- 本表在完整校对稿/冻结前只登记「本地按草案 §4 自检发现的前端实现差异 + 契约 v1.1 已确认的字段修正」，**不是冻结结论**。

## 1. 逐端点比对

> **口径**：下表为 dev-D 按《接口契约_家属端数据》v1.1 草案 §4 的**本地自检**，不是 B-3 校对结论；B-3 完整校对稿未见、X3 冻结未排期前，真实分支继续保持「待冻结」。

| 端点 | 核对项 | 草案出处 | 前端实现（`family/api.ts`） | 本地自检结论 | 差异 | 处置 |
| --- | --- | --- | --- | --- | --- | --- |
| F0 | `GET /api/family/elders`，响应只读 `{items}`；`BoundElder{elder_id,name,relation}` | §4 F0 | 只读 `data.items`，字段原样用于切换器 | 一致 | 无 | 待 B-3 复核 |
| F1 | `GET /api/family/elders/{elder_id}/reports?page=1&page_size=100` | §4 F1、S-10 | 显式分页参数，读 `Paged.items` | 一致 | 无 | 待 B-3 复核 |
| F2 | `GET /api/family/reports/{report_id}`；`medication_stats` 四键、`audio_url` P0 恒 null | §4 F2、FR-RPT-04 | 四键分列展示；不渲染播放器 | 一致 | 无 | 待 B-3 复核 |
| N1 | `GET /api/family/notifications?page=1&page_size=100&unread_only=false`；`payload` 未知键忽略 | §4 N1、§2.2 | 只消费 `elder_name`/`event_brief`/`report_id` | 一致 | 无 | 待 B-3 复核 |
| N4 | `POST /api/family/notifications/{id}/read`；幂等只写 `read_at` | §4 N4、S-03 | Mock 与真实分支均只返回/写 `read_at` | 一致 | 无 | 待 B-3 复核 |

> 关系判定：长者切换器数据源只来自 F0；当前身份范围由 `GET /api/sessions/current`（E3 `scope_elder_ids`）在入口层恢复；代码 0 处硬编码 `elder_id` 作为关系判定依据（裁决 A-01）。

## 2. 本地自检登记（非 B-3 结论，只登记不私改）

| # | 行类别（字段/判定/自裁） | 事项 | 草案出处 | 前端现状 | 处置 |
| --- | --- | --- | --- | --- | --- |
| L1 | 字段 | F0 响应外壳：契约只给 `{items}`，前端曾按 `{items,total,page,page_size}` 解析 | §4 F0 | 已按契约改为只读 `items`（真实分支标注待冻结） | 已在代码对齐；待 B-3 复核 |
| L2 | 字段 | F1/N1 分页参数：契约默认 1/20、上限 100 | §4 F1/N1、S-10 | 真实分支显式带 `page=1&page_size=100`（N1 另带 `unread_only=false`） | 已对齐；待 B-3 复核 |
| L3 | 判定 | F2 `audio_url` P0 恒 null、不渲染播放器 | §4 F2、FR-RPT-04 | 页面不渲染播放器 | 一致；待 B-3 复核 |
| L4 | 字段 | `payload` 未知键忽略不报错 | §2.2 | 前端只读 `elder_name/event_brief/report_id`，未知键不消费 | 一致；待 B-3 复核 |
| L5 | 自裁 | N4 幂等只写 `read_at`、不动 `status` | §4 N4、S-03 | Mock 与真实分支均只写 `read_at` | 一致；待 B-3 复核 |
| L6 | 自裁 | K1 开关口径：环境开关 vs S-11 常量 | §6 S-11 | 维持 `api.ts` 唯一 `USE_MOCK` 常量 | 差异已登记回传 dev-B（N4） |
| L7 | 字段 | `elder_guardians` 补 `id` uuid PK / `assigned_at` timestamptz | §2 R3 / TD 389/394 | F0 只消费 `elder_id`/`name`/`relation`，不消费 `id`/`assigned_at`；端点裁剪不变（S-05） | 契约 v1.1 已修正；前端无需改代码 |
| L8 | 自裁→字段原文 | S-01 `medication_stats` 四键改为 TD 596 原文，不再算自裁 | §2.1 / §6 S-01 | 前端四键 `taken_reported/refused/missed_unconfirmed/notification_failed` 与 TD 原文逐字一致 | 契约 v1.1 已修正；前端无需改代码 |

> 三行分类说明：`字段` 只来自 TD §3；`判定` 只来自 PRD FR/AC；`自裁` 只来自契约 §6。本表不把自裁行写成字段行或判定行（对应 S7 A1 自检项）。契约 v1.1 把 S-01 从「自裁」修正为「TD 原文」，L8 已按原文归类。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | G0 建表；登记本地自检 6 条（L1–L6），B-3 逐端点结论全部 ⏳ |
| v1.1 | 2026-11-07 | N7 完成后复核 L1–L6：为每行补「字段 / 判定 / 自裁」类别（A1 自检），L1 标记为本次代码已对齐；逐端点 B-3 结论仍全部 ⏳，未私改契约正文 |
| v1.2 | 2026-11-07 | merge origin/develop 后接收契约 v1.1：X2 改为「部分闭环」（R3 补 id/assigned_at、S-01 改 TD 原文；独立校对意见稿未见）；新增 L7/L8；逐端点结论仍待完整校对稿/X3，真实分支继续待冻结 |
| v1.3 | 2026-11-08 | 状态词规范化：改为「进行中（B-3 v1.1 已到，X3 冻结未排期）」；逐端点结论与真实分支「待冻结」标注不变（X3 仍未排期） |
| v1.4 | 2026-11-08 | 补 §1 逐端点本地自检（F0/F1/F2/N1/N4 各一行，明确「非 B-3 结论」）；5/5 端点与草案 §4 形状一致、差异 0；真实分支仍「待冻结」，契约正文零改动 |
