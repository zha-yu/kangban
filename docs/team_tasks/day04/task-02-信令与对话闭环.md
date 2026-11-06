# Task 02 · 信令追加与对话闭环（A2，W3）

> 状态：进行中　｜　版本：v1.9　｜　日期：2026-11-06
> 关联：docs/team_tasks/day04/README.md（分工总览与 C5′/C6 契约）｜docs/plans/day-04-3D与身份闭环.md §1.3/§2.1/§3.1
> 上游依据：PRD v2.6 FR-DLG-05（验收）｜技术设计文档 v1.5（实现）｜计划 T4/T6
> 变更记录见文末

## 职责与写作用域

- `app/audio/signaling.py`：WS 消息表追加——`identity_ask/identity_confirm`、`client_diag`（仅日志）
- `app/services/llm_service.py`：httpx 直连统一 LLM 适配器、超时/退避 ≤2、降级模板话术
- `app/services/dialogue_service.py`：Redis 最近 5 轮
- `app/core/context.py`：固定前缀人设 + 上下文组装
- `app/main.py`：仅路由装配行（本卡对 main.py 的唯一合法改动）
- greeting 开场白 + 消费 A1 `identity_service` 返回的 thread（A2 不建 `conversation_threads`）

**禁触**：voiceprints 域、frontend、迁移文件。Settings 新字段（`LLM_*`/`ENCRYPTION_KEY`）报 A3 统一录入。

## 前置依赖

Day 3 C2 WS 消息表（已冻结）；**C5 已定稿**（[contracts.md](contracts.md) v1.0，含五项开放点裁决），本卡为 C5/C6 冻结人，开工后签字确认并把契约原样拷入 `app/audio/signaling.py` 与 `app/services/dialogue_service.py`。比对待产时可先按 Mock 比对结果开发（C5 契约已解耦，spike 结论不阻塞本卡）。

**契约变更留痕**：identity/client_diag 属 Day 3 冻结契约的追加，须先在 `docs/plans/task-a2-*.md` 补契约变更记录再合入。

## 交付物

1. signaling.py 追加：`identity_ask{reason: low_confidence|vp_unavailable, candidates:[绑定长者]}`、`identity_confirm{elder_id}`（候选集外一律按未确认）、`client_diag`；确认 30s 超时 → generic
2. `llm_service`：**LLM_PROVIDER=minimax（OpenAI 兼容端点，2026-11-06 主理人已确认 key 到位）**，httpx 直连不引 SDK；首 token ≤800ms 预算、超时/限流退避重试 ≤2 后降级模板话术（FR-DLG-05）
3. `dialogue_service` + `context.py`：固定前缀 + Redis 最近 5 轮（Day 4 无记忆来源，自然满足「不读记忆」）
4. greeting：取 `personas.greeting` 送 TTS，不依赖 LLM（「5 秒立起并开口说话」）
5. thread 消费：elder_id/thread_id 全程服务端注入；换人由 A1 `identity_service` 结束旧 thread/开新 thread，A2 只消费 A1 `IdentityDecision` 的终态 `thread_id`（经 `identity_decision_to_result` 投影），不建 thread、不双写；A1 当前未回传 `session_seq`，A2 不伪造

## 验收标准（自验后再进联调窗口）

```
LLM 注入不可用 → 退避 ≤2 后降级话术，页面不白屏不静默
generic 模式回复不含任何个人记忆；用药/健康动作拒绝（FR-VPR-03）
换人说话 → A1 identity_service 结束旧 thread、开新 thread，A2 只消费新 thread、不串旧历史
identity_confirm 传候选集外 elder_id → 按未确认处理
uv run pytest -q 全绿；ruff check app 通过
```

## 变更记录

| v1.0 | 2026-11-06 | 初稿：自 day04 README v1.0 §3/§6 拆出 |
| v1.1 | 2026-11-06 | 主理人确认：LLM key 已到位，供应商定 **minimax**（OpenAI 兼容端点，`LLM_PROVIDER` 走泛化分支，适配器仍 httpx 直连不引 SDK）；真链路可直接开发与验收 |
| v1.2 | 2026-11-06 | 主理人裁决：`conversation_threads` 由 A1 `identity_service` 独占创建/结束；C6 `ThreadInfo.elder_id` 改为 `str | None`；A2 消费面改为单一 `IdentityThreadResult`（身份 + thread），不建 thread、不双写；任务卡状态改为进行中 |
| v1.3 | 2026-11-06 | A1 `identity_service` 实际交付 `IdentityDecision(source/elder_id/thread_id/reason/confidence/started_at)`，未回传 `ThreadInfo` 或 `session_seq`；A2 新增 `identity_decision_to_result` 适配，终态只消费 `thread_id`，`needs_confirm` 抛 `IdentityConfirmationRequired`；`session_seq` 留空不伪造，回传方式待 A1/主理人确认 |
| v1.4 | 2026-11-06 | B3/B4 完成：`app/main.py` lifespan 创建并关闭 Redis/LLM/Dialogue/Orchestrator 运行时；`signaling.dispatch_asr_event` + `set_dialogue_final_handler` 成为 FINAL 唯一 awaitable seam；`webrtc._asr_callback` 从 VAD segment 生成 WAV 并 await，字幕先投递一次，PARTIAL/ERROR 不触发对话；新增 `tests/test_dialogue_integration.py` |
| v1.5 | 2026-11-06 | C5 接线完成：voice/generic 终态发布 `identity_result`；confirm/timeout handler 调 A1 开 thread 并缓存；A1 开 thread 前先关闭该设备旧 thread；新增 `apply_generic_identity`、`begin_identity_recheck`、C5 handler 与 thread 生命周期测试 |
| v1.6 | 2026-11-06 | 换人检测修正：A1 已识别会话低于阈值 → 结束旧 thread + `confirm/low_confidence`，generic 命中 → voice 新 thread；A2 复检分支清缓存并发 `identity_ask`；补 5 类 recheck 测试 |
| v1.7 | 2026-11-06 | `session_seq` 与 TTS 音频落地：A1 回传 `session_seq`，A2 `IdentityThreadResult.session_seq` 不再为 None；回复先发 `tts_text`，再用 TTS Provider 合成 PCM 并通过 `webrtc.enqueue_tts_for_device` 写入播放轨；TTS 失败保留字幕 |
| v1.8 | 2026-11-06 | 合并 `origin/dev`（293881d，merge cb177db）后的 A2 兼容：`client_diag{stage:vad, ok:false}` 激活远端 W4 `VadFallbackController`；C5 identity 三打点位接入；`webrtc.py` 本地 `VadSession` 与远端 `VadFallbackController` 共存合并；A2 聚焦用例 111 passed / 1 skipped（C5 真库链路因 PostgreSQL 不可达 skip） |
| v1.9 | 2026-11-06 | W4 兜底段下游接线：`_handle_vad_segment` 无外部回调时默认跑 ASR，FINAL 经 `on_asr_event` 复用 A2 `_asr_callback`（字幕/dialogue handler/WAV 声纹音频）；兜底 turn 与 `_playback_turn_id` 对齐保证 TTS 写轨；客户端 turn 进行中或非 OPEN 跳过；兜底激活时停用本地 `VadSession`，避免双 VAD；新增 4 条测试，A2 聚焦 126 passed / 1 skipped |
