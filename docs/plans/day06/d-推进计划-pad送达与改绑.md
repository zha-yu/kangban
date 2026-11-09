> 状态：草稿　｜　版本：v1.0　｜　日期：2026-11-09
> 关联：docs/plans/day06/day-06-长者侧Agent闭环.md（主计划，节点规格唯一事实源）｜docs/plans/day06/d-DAG.md｜docs/team_tasks/day06/dev-d-pad送达确认与改绑UI.md（任务卡 v1.1）｜docs/team_tasks/day06/contracts.md（冻结契约 v1.0）
> 上游依据：PRD v2.9 §6.1 Day 6 行（验收）｜技术设计文档 v2.0 §8.3（实现）｜contracts.md §2/§4/§6（字段唯一口径）
> 变更记录见文末

# dev-D Day 6 工作推进计划 · pad 送达确认与 console 改绑

> 本计划是 dev-D 的**个人实施计划**（落位 `docs/plans/day06/`，依 AGENTS §10.1）。节点内容、量化验收以主计划 §3.1 与 dev-D 任务卡为准，本文只回答：**怎么分步做、每步交付什么、每步怎么自检、在哪一步和谁交接**。
> 执行纪律：**每完成一个 Step 即暂停汇报，经主理人确认后进入下一步**（主理人 2026-11-09 要求）。

---

## 1. 任务定位与开工基线

### 1.1 负责范围（合计 10.0 人时 + 接件 1.0）

| 节点 | 内容 | 档 | 人时 | 硬依赖 | 当前状态 |
| --- | --- | --- | --- | --- | --- |
| N03 | WS 消息契约代码同步（`tts_text` v2 + `segment_ack`） | S | 2.0 | N00 | 可立即开工 |
| N16 | pad 分段播放、ack 上报、打断收口与 epoch | S | 4.5 | N03、N07(dev-C) | N07 未开工，先做前端侧 |
| N18 | a0 console 改绑 UI | M | 3.5 | N19(dev-B)、契约增补 | N19 未开工，先做壳 |
| N22 子项 | 讲义作业「上下文样例」（纯文档） | L | 1.0 | N06(dev-B) | 与 dev-B 确认落点后写 |

### 1.2 开工基线核对（2026-11-09 实测）

| 项 | 结论 | 证据 |
| --- | --- | --- |
| G0 门禁 | ✅ 已过 | `contracts.md` v1.0 已冻结，PRD v2.9 / TD v2.0 / ER v0.5 修订记录齐 |
| 三条迁移 | ✅ 已 apply（0008/0009/0010） | `docs/team_tasks/day06/contracts.md` §7 |
| `tts_text` 现状 | 整段播、无分段字段 | `frontend/src/pad/protocol.ts` L150-153、`app/audio/signaling.py` L204-212 |
| 打断现状 | speechstart 本地停播 + `client_diag playback` 结算 | `frontend/src/pad/realtime.ts` L186-199、L228-234 |
| console 改绑入口 | **不存在员工列表**，`AdminUsersPage` 只有建号/家属三卡 | `frontend/src/console/pages/AdminUsersPage.tsx` |
| 员工 GET 端点 | **无** `GET /api/staff`，`StaffResponse` 不含 `elder_ids` | `app/api/staff.py` 仅 POST/PATCH；`app/api/schemas/staff.py` L41-48 |
| N03 契约起草 | **已在 N00 完成**：`contracts.md` §4.1/§4.2 已含全部字段 | N03 剩余工作仅为**代码侧同步**（contracts.md 不再改） |

### 1.3 本计划新增裁决（主理人 2026-11-09 已确认，登记备查）

| # | 事项 | 裁决 |
| --- | --- | --- |
| Q-1 | N18 契约缺口（无员工列表端点、无当前绑定查询） | **提契约增补并通知 dev-B**：dev-D 起草提案 → dev-B + 主理人评审 → dev-B 改契约与后端，dev-D 拿端点做 UI。详见 Step S5 |
| Q-2 | 分段音频边界判定 | **「下一段到达即结算」**：收到第 N 段 `tts_text` 时，第 N-1 段上报 `played`、第 N 段上报 `playing`；末段以轨结束为 `played`。端到端口径列入 J3 与 dev-C 逐条确认 |
| Q-3 | dev-D 负载偏轻 | **接入 N22「上下文样例」作业**（纯文档，约 1.0h），落点与 dev-B（N22 负责人）确认 |

---

## 2. 分步执行计划（每步一个暂停确认点）

> 每步格式：**前置 → 操作步骤 → 交付物（格式 / 范围 / 质量红线）→ 自检标准 → ⏸ 暂停点汇报内容**。

### Step S1 · N03 消息契约代码同步（2.0h，可立即开工）

**前置**：G0 已过；`contracts.md` §4 为唯一口径（含 5 个可选字段——注意 dev-D 卡标题写「四字段」，以冻结契约为准，`total` 也在内）。

**操作步骤**：

1. 扩展 `frontend/src/pad/protocol.ts`：
   - `TtsTextMsg` 增 5 个可选字段：`response_id?` / `epoch?` / `segment_id?` / `index?` / `total?`（全部 `?`，老字段 `type/text/degraded` 语义与顺序不动）。
   - 新增 `SegmentAckMsg`（C→S）：`{ type: "segment_ack", response_id, epoch, segment_id, status: "playing"|"played"|"canceled", at_ms }`，并入 `ClientSignal` 联合类型。
   - 文件头注释补一行 Day 6 契约来源（contracts.md v1.0 §4）。
2. `app/audio/signaling.py` 的 TypedDict 同步：**dev-D 起草字段草案发给 dev-C，由 dev-C 落码**（写锁 L6：protocol.ts 消息段 dev-D 起草 → dev-C 复核；signaling.py 归 dev-C 的 N07 写作域）。dev-D 不直接改 `app/**`。
3. 0 基口径复核：`segment_id` 与 `index` 均 0 基、`index === segment_id`（contracts.md §4.1 已写死）。

**交付物**：`protocol.ts` 扩展（TS）；给 dev-C 的 signaling.py 字段草案（粘贴文本即可，不落文件）。

**质量红线**：新字段全部可选；字段名与 `contracts.md` §4 逐字一致；`parseServerSignal` 不受影响（未知 type 仍返回 null 不白屏）。

**自检**：`pnpm --dir frontend build` 零 TS 错误；`pnpm --dir frontend dev` 打开 pad 页，现有整段播放回归不受影响（老服务端消息无新字段时走原路径）。

**⏸ 暂停汇报**：diff 摘要 + build 结果 + 已发给 dev-C 的草案文本；等确认后进 S2。

---

### Step S2 · N16-a 播放队列 `playback-queue.ts`（约 2.0h）

**前置**：S1 已确认；N07 未就绪（服务端暂不发分段字段），因此队列必须**兼容整段模式**。

**操作步骤**：

1. 新建 `frontend/src/pad/audio/playback-queue.ts`：
   - `enqueue(segment: TtsTextMsg)`：入队（含 `response_id/epoch/segment_id/index/total`，任一缺失按整段处理）。
   - 旧 epoch 防护：`epoch < 已见最大 epoch` → 丢弃并记 `stale_segment_dropped`（client_diag 上报），不播不 ack；`epoch` 为客户端**原样回传**值，前端不自增。
   - 播放回调（Q-2 裁决口径）：段 N 入播 → 上报 `playing`（N）；收到段 N+1 → 段 N 上报 `played`；**末段** `played` 以轨结束（`audioEl` ended）触发；被打断 → 当前未结算段与队列剩余全部上报 `canceled`（带最后已确认 `played` 的 `segment_id`）。
   - ack 上报走「尽力而为 + 1 次重发」，**绝不在 `onended` 里 await 网络**；上报失败只打 `client_diag`，不阻塞播放。
2. ack 发送通道：经 `SignalSocket.send`（复用现有信令 WS，不新建连接）。

**交付物**：`playback-queue.ts`（TS，新增文件）。

**质量红线**：单段失败不影响后续段；Stale segment 被丢弃；无分段的旧消息 100% 走整段回退路径。

**自检**：单文件逻辑自审（在 N07 就绪前的联调只能在 S4 做）；`pnpm --dir frontend build` 零 TS 错误。

**⏸ 暂停汇报**：diff + 队列状态机说明 + 整段回退验证方式；等确认后进 S3。

---

### Step S3 · N16-b `realtime.ts` 接线与打断收口（约 2.5h）

**前置**：S2 已确认。

**操作步骤**：

1. `tts_text` 分支改接 `playback-queue`：有分段字段 → 入队；无 → 保留现整段播放路径（回退能力）。
2. C-6 计时合并：**一个段一次计时**，`playbackStartMs` 由队列的段起播驱动；打断与播完都结算（修 a1 基线表登记的「F5 早于 onended」缺口）。
3. 打断收口（顺序不可换，对齐 FR-UI-04）：停 `<audio>`（若仍有尾音 → `src=""` + `load()` 硬切）→ 清队列 → 上报 `canceled`（带最后已确认段）→ 沿用现有打断信令（speechstart 路径，**不新增消息**）。
4. 打断后 200ms 内静音（手工验证项）。

**交付物**：`realtime.ts` 改动（TS）。

**质量红线**：打断顺序固定；旧路径可回退；不改 `avatar/**`、`kws/**`、`voiceprint/**`（禁触）。

**自检**：`pnpm --dir frontend build` 零 TS 错误；本地起后端跑一遍整段对话回归（无分段字段）确认无回归。

**⏸ 暂停汇报**：diff 摘要 + 整段回归结果；等确认后进 S4。

---

### Step S4 · N16 端到端联调与 20 轮验证（约 1.0h 前端侧 + 等 dev-C）

**前置**：**dev-C 的 N07（ResponseStream）合入**——这是本步骤的硬外部依赖，未就绪则本步挂起，先推进 S5。

**操作步骤**（对应主计划 J3/J4 对接）：

1. 与 dev-C 逐条核对 J3 清单：① 字段表双向对照（protocol.ts ↔ signaling.py 零差异）；② 0 基 `segment_id` 双方确认；③ 末段 `played` 的触发机制（轨结束是否可靠，不可靠则请 dev-C 给出复用现有信号的结束口径——不改冻结契约优先）；④ 首段未播完即被打断时 `canceled` 的 `segment_id` 取值口径（契约未定义，需 dev-C 定）；⑤ 服务端 ack 校验规则与拒绝原因枚举。
2. 真实链路验证（留录屏/截图）：
   - 一段回复切 ≥2 段：服务端日志见 `segment_ack` 的 `playing`/`played` 成对；
   - 播第 1 段插话：页面立即停声，服务端 `delivered_text` 只到第 1 段；
   - 连说 20 轮：ack 上报率 ≥95%（不足则查 `onended` 是否被打断）。
3. 异常路径：服务端未发 `response_id`（回滚/旧后端）→ 整段回退不报错；迟到的旧 epoch 段被丢弃。

**交付物**：20 轮验证记录（截图/录屏 + 服务端事件摘录）。

**⏸ 暂停汇报**：J3 五项对齐结论 + 验证证据；等确认后进 S5。

> **联调提醒**：本步开始前需与 **dev-C** 预约双人在场时段（J3）。交接物与判据见 §3。

---

### Step S5 · N18-a 契约增补提案 + console API 壳（约 1.0h，可提前穿插）

**前置**：无硬依赖（不等 dev-B），可与 S2/S3 并行穿插。

**操作步骤**：

1. 起草契约增补提案（粘贴文本/评审用 Markdown，落点由主理人定，建议进 `docs/接口契约_身份与权限.md` 新端点 + 修订记录）：
   - `GET /api/staff`（admin only）：员工列表，返回 `staff_id / name / role / phone_masked / status / elder_ids[]`（当前负责长者，供「默认勾选」）。
   - 分页口径沿用 E7 现状（page/pageSize），脱敏口径与 E12 一致。
2. **通知 dev-B 与主理人**：提案文本 + 明确请 dev-B 评估实现成本（其 N19 正在做 `reassign_elders()`，顺手加 GET 成本最低）。dev-D 不改 `app/**` 与契约文件。
3. `frontend/src/console/admin-api.ts` 新增封装壳：`reassignElders(staffId, elderIds)`（E20 `PUT /api/staff/{staff_id}/elders`，200 返回 `{ staff_id, added[], removed[] }`）与 `listStaff()`（等契约增补冻结后联调）。403/409 文案按 dev-D 卡 §3.4 写死：403「只有管理员可以调整负责关系」、409「该长者将失去最后一位责任护工，请先指定接替人」。复用 `adminPost` 模式与 `AdminApiError`，**不新增第二套错误封装**。

**交付物**：契约增补提案文本；`admin-api.ts` 扩展（TS）。

**质量红线**：不臆造 `elder_id`；无组件库；E20 响应处理三态（200/403/409）齐备。

**自检**：`pnpm --dir frontend build` 零 TS 错误；E20 三态文案与 dev-D 卡 §3.4 逐字一致。

**⏸ 暂停汇报**：提案文本 + diff；**此处需主理人对契约增补拍板**，拍板后由 dev-B 排 GET 端点；等确认后进 S6。

---

### Step S6 · N18-b 员工列表 + 改绑弹窗（约 2.5h）

**前置**：S5 已拍板且 **dev-B 的 GET 端点已就绪**（硬依赖 N19）；未就绪则 UI 保持壳 + 禁用提交，**不得用假数据联调**。

**操作步骤**：

1. `AdminUsersPage.tsx` 新增「员工列表」卡：admin 登录可见，表格列（姓名/角色/脱敏手机号/负责长者数），每行「调整负责长者」按钮（仅 `role=admin` 可见；`care_worker/doctor` 行按钮隐藏——前端隐藏只是体验，权限以后端 403 为准）。
2. 改绑弹窗：长者多选数据来自真实 E7（复用页内已有 `listElders`），默认勾选来自 GET 端点的 `elder_ids`；提交调 `reassignElders`（全量覆盖）。
3. 响应三态：200 → 关弹窗、刷新列表、提示「已更新，新增 N 位、移除 M 位」（用后端 `added/removed`）；403/409 → §3.4 固定文案。
4. 前端**不做**「是否最后一位责任护工」的本地预判（以后端 409 为准，避免两处规则漂移）。
5. 样式纯原生 CSS + token 变量（`var(--color-*)`），沿用页内 `admin-card` 风格。

**交付物**：`AdminUsersPage.tsx` 扩展（TSX）；若需新增少量弹窗样式，进 `frontend/src/console/index.css`（本卡写作用域外，需先向主理人报备）。

**质量红线**：不写死 id；403/409 可读提示；无组件库；改绑后列表刷新用服务端返回差集而非本地推算。

**自检**：`pnpm --dir frontend build` 零 TS 错误；手工三路径各走一次（200/403/409，非 admin 账号确认按钮不可见且直接调接口 403）。

**⏸ 暂停汇报**：diff + 三路径手工截图；等确认后进 S7。

---

### Step S7 · N22 子项「上下文样例」作业（约 1.0h，纯文档）

**前置**：dev-B 的 N06（ContextBuilder）产出可用；落点先与 dev-B 确认（建议与其 N22 三件套同一文件或 `docs/plans/day06/` 下独立作业文件，文件名与 dev-B 商定后在本计划补登）。

**操作步骤**：按讲义 §12 作业要求，基于 N06 组装后的真实上下文输出，写一份带来源分级标注的样例（哪层来自安全策略/身份/活跃任务/历史/记忆，预算裁掉了什么）。

**交付物**：上下文样例文档（Markdown + 标注表）。

**自检**：样例与 N06 实际输出一致（可复核）；每条内容标注来源层。

**⏸ 暂停汇报**：文件路径 + 样例内容；等确认后进 S8。

---

### Step S8 · 收口：构建门禁与看板回填（约 0.5h）

**操作步骤**：

1. `pnpm --dir frontend build`：零 TS 错误，pad/console/family 三产物均出（主计划 V12）。
2. 任务卡回填：改 `docs/team_tasks/day06/dev-d-pad送达确认与改绑UI.md` 首部 `> 状态：` 为 `已完成`（pre-commit 钩子自动刷新 PROGRESS.md）。
3. 本计划状态行改 `已冻结`，并核对 `docs/plans/README.md` 索引行状态。
4. `uv run python scripts/update_progress.py --check` 退出码 0（主计划 V14）。

**⏸ 暂停汇报**：全部自检证据 + 缺口清单（如有）。

---

## 3. 联调与交接清单（谁、什么时候、交什么）

| 编号 | 时点 | 对象 | 我给出 | 我要到 | 完成判据 |
| --- | --- | --- | --- | --- | --- |
| J-C1 | S1 完成 | **dev-C** | `protocol.ts` 字段草案（signaling.py 侧由其落码） | 复核结论 | 两侧 TypedDict 字段名零差异 |
| **J-Contract** | S5 完成 | **dev-B + 主理人** | `GET /api/staff` 契约增补提案（端点形状 + `elder_ids` 返回字段 + 脱敏口径） | 拍板结论与 dev-B 的排期承诺 | 契约文件补增补 + 修订记录；dev-B 排期明确 |
| J3 | S4 开始前 | **dev-C**（双人在场） | 字段表、段结算口径（Q-2）、ack 触发时机 | 服务端 ack 校验四步与拒绝枚举；末段结束口径；首段打断 `segment_id` 口径 | 五项对齐点逐条有结论 |
| J4 | S4 中 | **dev-C** | ack 实际报文样例 | 服务端落库 `conversation_events` 样例 | 同一 `response_id` 下段有序 |
| J6 | S6 | **dev-B** | UI 侧 403/409 展示文案 | E20 端点与响应体实测 | 三条路径各走一次 |
| J-N22 | S7 前 | **dev-B** | 上下文样例写作意向 | N06 真实输出 + 作业落点约定 | 落点登记进本计划与 N22 |

> 联调纪律（沿用主计划）：契约字段只以 `contracts.md` 为准，口头约定无效；跨端问题由提出方带 `trace_id` 与请求/响应原文。

---

## 4. 风险与回退

| # | 风险 | 触发信号 | 处置 |
| --- | --- | --- | --- |
| R1 | dev-C N07 迟迟不合入，S4 挂起 | W3 末 N07 未绿 | 前端用本地 mock 服务端消息验证队列状态机；整段回退路径先行验收；S4 转次日 |
| R2 | 契约增补未拍板 / dev-B GET 排不上 | S5 后 24h 无结论 | N18 保持壳 + 禁用提交，员工列表转 Day 6.5；**不得造假数据** |
| R3 | 末段 `played` 轨结束不可靠（WebRTC 轨不触发 ended） | 20 轮 ack 率 <95% 且缺末段 | J3 请 dev-C 给复用现有信号的结束口径；过渡期服务端按「发送即视为 delivered」降级打 `delivery_status=unknown`（主计划回退条款，不写 `delivered`） |
| R4 | 分段导致听感断裂 | 手工试听断续 | 队列预取下一段、段间不留空；仍有断裂则合并相邻短段（需 dev-C 服务端配合） |
| R5 | ack 上报干扰播放 | 播放卡顿 | ack 异步 + 失败最多重发 1 次；绝不在 `onended` 里 await 网络 |
| R6 | 与 dev-C 抢 `signaling.py` | 双方同时改 | 我只起草草案，服务端分支由 dev-C 落（写锁 L6） |

---

## 5. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版：基于主计划 v3.0、dev-D 任务卡 v1.1、冻结契约 v1.0 与前端/后端现状实测（无员工列表端点、无当前绑定查询、`tts_text` 无分段字段）制定 S1–S8 八步计划；登记三项主理人裁决（Q-1 契约增补通知 dev-B、Q-2 下一段到达即结算、Q-3 接上下文样例）；新增 J-Contract 交接件与末段结束、首段打断两项 J3 待对齐口径 |
