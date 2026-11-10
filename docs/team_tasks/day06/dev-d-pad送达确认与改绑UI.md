> 状态：已完成　｜　版本：v1.2　｜　日期：2026-11-10
> 负责人：dev-D（前端 · pad 送达确认 + console 改绑）
> 关联：docs/plans/day06/day-06-长者侧Agent闭环.md（主计划 §3.1 节点表为唯一事实源）｜docs/team_tasks/day06/dev-a/dev-b/dev-c
> 上游依据：技术设计文档 line 573（媒体分发）、line 517（`work_orders.assignee_id`）、line 375-383（`elder_staff_assignments`）｜PRD line 250（FR-UI-04 打断）、line 254（FR-UI-08 撤销边界）、line 437（FR-ADM-03 服务端校验）、line 499（AC-18）
> 变更记录见文末

# Task dev-D · pad 送达确认与 console 改绑（Day 6）

## 1. 目标与边界

**一句话**：让服务端知道"到底播了多少"，并补上权限线唯一的写入口缺口。

**负责节点**：N03、N16、N18，合计 10.0 人时。**全队唯一前端位**，两件事互不依赖，可交叉推进。

> 计划 v2.0 起，原 N17（待答提示 UI）已取消，其**打断收口**部分并入 N16；`task.waiting_for` 的后端语义由 dev-B 的 N05/N06 覆盖，前端不再做提示条。

**做**

- WS 消息契约扩展：C→S `segment_ack`，S→C `tts_text` v2 四字段（与 dev-C 共锁）。
- pad 分段播放、ack 上报与打断收口（含 `canceled` 上报）。
- a0 console `AdminUsersPage` 的"调整负责长者"。

**不做**

- 不改 `frontend/src/family/**`（本日无家属端任务）。
- 不引第三方组件库（`AGENTS.md` §1：console 用原生 CSS + token 变量）。
- 不在前端做权限判定（FR-ADM-03：隐藏按钮不算权限，后端必须能 403）。
- 不改 `app/**` 业务逻辑；`signaling.py` 的服务端分支由 dev-C 落（L6 写锁）。

**写作用域（白名单）**

```
frontend/src/pad/protocol.ts               扩展消息类型
frontend/src/pad/realtime.ts               分段播放与 ack、打断收口
frontend/src/pad/audio/playback-queue.ts   新增
frontend/src/console/pages/AdminUsersPage.tsx     扩展改绑操作
frontend/src/console/api.ts                       新增 reassign 调用（若已有 api 层）
docs/team_tasks/day06/contracts.md                消息契约章节（起草）
```

**禁触**：`frontend/src/pad/avatar/**`、`kws/**`、`voiceprint/**`（Day 4–5 已验收，回归风险高）；`app/**`。

## 2. 节点清单

| 节点 | 任务 | 人时 | 输入 | 输出 | 依赖 | 量化验收 |
| --- | --- | --- | --- | --- | --- | --- |
| N03 | WS 消息契约扩展 | 2.0 | 讲义 §5.3；`protocol.ts`；`signaling.py` | 双向字段表 + 事件枚举对齐 | N00 | `protocol.ts` 与 `signaling.py` TypedDict 字段名零差异；老字段不动 |
| N16 | pad 分段确认、打断收口与 epoch | 4.5 | N03、N07 | 播放队列 + ack 上报 + 打断路径 | N03、N07 | 20 轮 ack 上报率 ≥95%；打断 200ms 内停声；旧 epoch 不串音 |
| N18 | console 改绑 UI | 3.5 | N19 端点 | 操作列 + 长者多选弹窗 | N19 | 403/409 有可读提示；不臆造 `elder_id` |

## 3. 逐节点执行步骤

### 3.1 N03 · 消息契约（先冻结再动手）

1. S→C `tts_text` 扩展（**向后兼容，老字段顺序不变**）：

```ts
{ type: "tts_text", text: string, degraded: boolean,
  response_id?: string, epoch?: number, segment_id?: number, index?: number, total?: number }
```

> 可选字段的理由：Day 5 已上线的 pad 若拿到多余字段必须不报错；缺失时按"整段一播"处理。

2. C→S 新增：

```ts
{ type: "segment_ack", response_id: string, epoch: number,
  segment_id: number, status: "playing" | "played" | "canceled", at_ms: number }
```

3. 契约纪律：字段只以 `contracts.md` 为准；`epoch` 是**客户端原样回传**服务端下发的值，**前端不得自增**（自增会导致服务端无法淘汰迟到回调）。
4. 与 dev-C 对齐点：`segment_id` 从 1 开始还是 0 开始、`index` 与 `segment_id` 是否同值——**统一取 0 基 `segment_id` + 0 基 `index`**，写进契约，避免 1/0 混用。

### 3.2 N16 · 分段播放与 ack

1. 新增 `audio/playback-queue.ts`：入队（segment）→ 顺序播 → 每段 `onstart` 上报 `playing` → `onended` 上报 `played` → 被打断上报 `canceled`。
2. 上报失败不阻塞播放：ack 走"尽力而为 + 1 次重发"，失败只打 `client_diag`，**不得**因为这个让播放卡住。
3. 旧 epoch 防护：收到 `tts_text` 时若 `epoch < 当前已见最大 epoch`，直接丢弃并记 `stale_segment_dropped`；这是"不串音"的前端最后一道闸。
4. 与现有 `realtime.ts` 的 `playback` 计时（C-6）合并：**一个段一次计时**，不重复起表；`a1-延迟基线表` §5-1 的 playback 缺口正是因为 F5 早于 `onended`，本次要求"打断与播完都结算"。
5. 兼容路径：若服务端未下发 `response_id`（回滚或旧后端），退化为当前整段播放逻辑，不报错。

### 3.3 N16 内嵌 · 打断收口

> 原 N17 的打断部分并入本节；"待答提示 UI"已随主计划 v2.0 取消。

1. 打断路径顺序（与 FR-UI-04 一致，**顺序不可交换**）：停 `<audio>` → 清队列 → 上报 `canceled` → 通知服务端（沿用现有打断信令，不新增消息）。
2. `canceled` 必须带当前 `response_id` 与**上一个成功播放段的 `segment_id`**，服务端据此截断 `delivered_text`；不带则服务端只能整轮作废，会把没播的算成已播。
3. 打断后 200ms 内必须静音；若 `<audio>.pause()` 后仍有尾音，改为 `src=""` + `load()` 硬切。
4. **不做**：待答提示条、`TaskHint` 组件、字号/触控区适配（原 N17 第 2–4 条，随节点取消）。

### 3.4 N18 · console 改绑

1. 入口：`AdminUsersPage` 员工列表每行加"调整负责长者"按钮，仅 `role=admin` 可见。
2. 弹窗：长者多选（数据来自真实 E7 候选长者接口，**不得写死 `elder_id`**），默认勾选当前绑定；提交调 `PUT /api/staff/{staff_id}/elders`（全量覆盖）。
3. 响应处理：
   - `200` → 关闭弹窗、刷新列表、提示"已更新，新增 N 位、移除 M 位"（用后端返回的 `added/removed`）。
   - `403` → "只有管理员可以调整负责关系"。
   - `409` → "该长者将失去最后一位责任护工，请先指定接替人"。
4. 纯原生 CSS + token 变量（`var(--color-*)`），不引组件库。
5. 前端不做"是否最后一位"的本地预判——以后端 409 为准，避免两处规则漂移。

## 4. 交付物规格

| 交付物 | 格式 | 质量红线 |
| --- | --- | --- |
| `protocol.ts` 扩展 | TS | 新字段全部可选；类型与 `signaling.py` 同名同义 |
| `playback-queue.ts` | TS | 单段失败不影响后续段；`Stale segment` 被丢弃 |
| `realtime.ts` 改动 | TS | 打断顺序固定；旧路径可回退 |
| `AdminUsersPage.tsx` | TSX | 403/409 有可读提示；不写死 id；无组件库 |
| `contracts.md` 消息章节 | Markdown | 字段名/类型/可空/枚举逐行写死 |

## 5. 自检

```bash
pnpm --dir frontend build                      # 零 TS 错误，三入口产物均出
pnpm --dir frontend dev                        # 手工：喊小伴→说两句→插话打断
```

手工判据（必须留截图或录屏）：

1. 一段回复被切成 ≥2 段时，服务端日志能看到 `segment_ack` 的 `playing`/`played` 成对出现。
2. 播到第 1 段插话：页面立即停声，服务端 `delivered_text` 只到第 1 段。
3. 连说 20 轮，ack 上报率 ≥95%（不足则查 `onended` 是否被 F5 打断）。
4. console：admin 把某长者责任护工 A→B，A/B 各自刷新列表范围随之变化；非 admin 该按钮不可见且直接调接口 403。

## 6. 对接与联调

| 时点 | 对象 | 我给出 | 我要到 | 判据 |
| --- | --- | --- | --- | --- |
| J3 W3 末 | dev-C | `protocol.ts` 字段定义、`segment_ack` 触发时机 | 服务端 ack 校验规则与拒绝原因枚举 | 字段逐个对照；0 基 `segment_id` 双方确认 |
| J4 后 | dev-C | ack 上报的实际报文样例 | 服务端落库后的 `conversation_events` 样例 | 同一 `response_id` 下段有序 |
| J6 W6 末 | dev-B | UI 侧 403/409 展示文案 | E20 端点与响应体 | 三条路径各走一次 |

## 7. 风险与回退

| 风险 | 处置 |
| --- | --- |
| 分段导致听感断裂 | 队列预取下一段（`preload` 下一段音频），段间不留空；若仍有断裂则合并相邻短段 |
| ack 上报干扰播放 | ack 走异步、失败不重试超过 1 次，绝不在 `onended` 里 await 网络 |
| 与 dev-C 抢 `signaling.py` | 我只起草契约，服务端分支由 dev-C 落（L6） |
| a0 端点未就绪 | UI 先做壳 + 禁用提交，**不得**用假数据联调 |

## 8. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版：按主计划 v1.0 拆 4 个节点；含 `tts_text` v2 兼容策略、`segment_ack` 五字段、0 基 `segment_id` 约定、打断顺序、a0 三态响应文案 |
| v1.1 | 2026-11-09 | 随主计划 v2.0 收紧：**删除 N17**（待答提示 UI），打断收口并入 N16（4.0→4.5h）；移除 `TaskHint.tsx` 与 `App.tsx` 接线、写作用域与风险各一条；节点 4→3，本卡合计 12.5→10.0 |
| v1.2 | 2026-11-10 | 状态 未开始→**已完成**（S1–S4/S6–S8 全部执行）：契约代码同步、播放队列分段+ack、打断收口（canceled 双口径实测）、20 轮验证 ack 54/54=100%（判据 3 达标 ≥95%）、console 改绑 UI、上下文样例底稿、build 门禁通过。遗留：①真人停声复测与 TEN-VAD 漏检复现**延后**（主理人 2026-11-10 指示），判据 2「delivered_text 只到第 1 段」因 dev-C D1 缺陷（turn 终态全量不落库）暂不可验；②D1/D2 转 dev-C、D4 转 dev-A+C，见 docs/plans/day06/d-s4-20轮验证记录.md §4 |
