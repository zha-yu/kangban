# Task 02 · 信令追加与对话闭环（A2，W3）

> 状态：未开始　｜　版本：v1.0　｜　日期：2026-11-06
> 关联：docs/team_tasks/day04/README.md（分工总览与 C5′/C6 契约）｜docs/plans/day-04-3D与身份闭环.md §1.3/§2.1/§3.1
> 上游依据：PRD v2.6 FR-DLG-05（验收）｜技术设计文档 v1.5（实现）｜计划 T4/T6
> 变更记录见文末

## 职责与写作用域

- `app/audio/signaling.py`：WS 消息表追加——`identity_ask/identity_confirm`、`client_diag`（仅日志）
- `app/services/llm_service.py`：httpx 直连统一 LLM 适配器、超时/退避 ≤2、降级模板话术
- `app/services/dialogue_service.py`：Redis 最近 5 轮
- `app/core/context.py`：固定前缀人设 + 上下文组装
- `app/main.py`：仅路由装配行（本卡对 main.py 的唯一合法改动）
- greeting 开场白 + thread 闸门①（服务端建 `(elder_id, session_seq)`）

**禁触**：voiceprints 域、frontend、迁移文件。Settings 新字段（`LLM_*`/`ENCRYPTION_KEY`）报 A3 统一录入。

## 前置依赖

Day 3 C2 WS 消息表（已冻结）；**C5 已定稿**（[contracts.md](contracts.md) v1.0，含五项开放点裁决），本卡为 C5/C6 冻结人，开工后签字确认并把契约原样拷入 `app/audio/signaling.py` 与 `app/services/dialogue_service.py`。比对待产时可先按 Mock 比对结果开发（C5 契约已解耦，spike 结论不阻塞本卡）。

**契约变更留痕**：identity/client_diag 属 Day 3 冻结契约的追加，须先在 `docs/plans/task-a2-*.md` 补契约变更记录再合入。

## 交付物

1. signaling.py 追加：`identity_ask{reason: low_confidence|vp_unavailable, candidates:[绑定长者]}`、`identity_confirm{elder_id}`（候选集外一律按未确认）、`client_diag`；确认 30s 超时 → generic
2. `llm_service`：**LLM_PROVIDER=minimax（OpenAI 兼容端点，2026-11-06 主理人已确认 key 到位）**，httpx 直连不引 SDK；首 token ≤800ms 预算、超时/限流退避重试 ≤2 后降级模板话术（FR-DLG-05）
3. `dialogue_service` + `context.py`：固定前缀 + Redis 最近 5 轮（Day 4 无记忆来源，自然满足「不读记忆」）
4. greeting：取 `personas.greeting` 送 TTS，不依赖 LLM（「5 秒立起并开口说话」）
5. thread 闸门①：elder_id 全程服务端注入，换人 → 旧 thread `ended_at` 落库 + 新 thread

## 验收标准（自验后再进联调窗口）

```
LLM 注入不可用 → 退避 ≤2 后降级话术，页面不白屏不静默
generic 模式回复不含任何个人记忆；用药/健康动作拒绝（FR-VPR-03）
换人说话 → 旧 thread ended_at 落库、新 thread 开启，内容互不渗漏
identity_confirm 传候选集外 elder_id → 按未确认处理
uv run pytest -q 全绿；ruff check app 通过
```

## 变更记录

| v1.0 | 2026-11-06 | 初稿：自 day04 README v1.0 §3/§6 拆出 |
| v1.1 | 2026-11-06 | 主理人确认：LLM key 已到位，供应商定 **minimax**（OpenAI 兼容端点，`LLM_PROVIDER` 走泛化分支，适配器仍 httpx 直连不引 SDK）；真链路可直接开发与验收 |
