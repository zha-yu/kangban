> 状态：已冻结　｜　版本：v1.0　｜　日期：2026-11-11
> 关联：docs/team_tasks/day08/dev-D-日报通知API与family-pad.md（D6）｜docs/plans/day07/d-联调记录.md L6#10（裁决出处）｜frontend/src/pad/protocol.ts（消费契约先例：day06 contracts.md v1.0 §4）
> 上游依据：day07 联调记录 L6#10 主理人裁决「改法②：协议加独立路由降级标记字段」（2026-11-11 转入 D6 前置）；PRD §3.2 身份与降级口径不涉及本字段
> 变更记录见文末

# 契约冻结：tts_text 增 route_degraded 字段（D6 前置）

## 一、冻结范围与生效

- **冻结对象**：WS 下行消息 `tts_text`（`frontend/src/pad/protocol.ts` `TtsTextMsg`）新增可选字段 `route_degraded`。
- **生效约束**：冻结后字段名、类型、语义**不得私改**（同 D4「只按 X8 冻结 schema/path 消费」纪律）；如需变更走新裁决 + 本文档修订记录。
- **消费方**：dev-D（D6：`protocol.ts` 增字段 + `realtime.ts` 触发分支 + 测试）；**产出方**：dev-B（服务端 tts_text 组装处下发）。

## 二、字段口径（冻结内容）

```ts
export interface TtsTextMsg {
  // …既有字段不动…
  /** 路由降级标记：L0/L1 不可用经 fallback 由 L2 Agent 正常生成时为 true；
   *  与 degraded 正交（degraded=回复本身是降级模板话术）。冻结 v1.0（L6#10 裁决）。 */
  route_degraded?: boolean;
}
```

| 口径项 | 冻结值 | 说明 |
| --- | --- | --- |
| 字段名 | `route_degraded` | 沿用 L6#10 建议名，不改 |
| 类型 | `boolean`（可选字段） | TS 侧 `route_degraded?: boolean` |
| 下发时机 | **每条 `tts_text` 恒下发**（显式 `true`/`false`，不缺省） | 客户端不做「缺省即 false」推断，避免旧消息混流歧义 |
| `true` 条件 | 本轮路由发生 fallback（L0/L1 不可用）→ L2 Agent **正常生成**回复 | 对应 day07 实测场景：sidecar 杀掉走 fallback、Agent 正常回复 |
| `false` 条件 | 正常路由轮（L0/L1 命中或 L1 直路由成功） | clarify / 拒答 / 失败轮同样 `false`，提示条不显示 |
| 与 `degraded` 关系 | **正交、互不推断**：`degraded=true` ⇒ 回复是降级模板（LLM/工具失败）；`route_degraded=true` ⇒ 回复正常但换了路由层级 | 两者可同时为 true（路由降级 + 生成也降级），提示条以 `route_degraded` 为准 |
| 前端判定 | `realtime.ts` 提示条触发条件改为 `msg.route_degraded === true` | 既有 `msg.degraded === true` 分支保留（降级模板话术仍走原提示路径） |
| 文案 | 「网络慢，正在换种方式回答」 | 沿用 day07 L5-3 预期文案，不在本冻结范围调整 |
| 服务端落点 | `turn_service` tts_text 组装处按轮内路由结果置位；组装函数签名不变 | B 域实现，fallback 判定来源=既有路由层结果，不新增探测逻辑 |

## 三、验收对照（D6 验收标准引用）

1. fallback 轮（杀 sidecar）：pad 收到的 `tts_text.route_degraded === true`，显示提示条；Agent 回复正常播放（AC 对齐 day07 L5-3）。
2. 正常轮：`route_degraded === false`，提示条不出现。
3. LLM 失败降级轮：`degraded === true && route_degraded === false`，走原降级话术路径。
4. `protocol.ts` / `realtime.ts` 增量通过 `pnpm --dir frontend exec tsc --noEmit`。

## 四、变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-11 | 首版冻结：依据 day07 L6#10 裁决（协议加字段），D6 开工前置；字段名/类型/恒下发口径/与 degraded 正交关系/前端判定共八项冻结 |
