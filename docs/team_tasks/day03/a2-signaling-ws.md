# A2 · 信令 WS 与鉴权

> 状态：已完成　｜　版本：v1.2　｜　日期：2026-11-05
> 关联：docs/team_tasks_day03/README.md（分工总览）
> 上游依据：day-03 计划 T2（WS 信令+设备 JWT 鉴权）｜adhoc 方案 M2/M3｜接口契约_身份与权限.md E4
> 变更记录见文末

## 职责与写作用域

- `app/audio/signaling.py`：WS `/ws/signal` 全部实现
- `app/main.py`：仅追加一行路由装配（本单元对 main.py 的唯一合法改动）

**禁触**：`app/core/auth*`（只调用既有 `verify_device_token`，不改动）、媒体层、frontend。

## 前置依赖

Day 2 设备 JWT 已就绪（`POST /api/device-sessions`，E4）。**产出 C2 契约**（WS 消息表），09:30 前定稿，A3/A4 据此开发。

## 交付物

1. 首帧鉴权：连接后 300ms 内必须收到 `auth{token}`，超时/伪造 → `4401` 关闭 + 审计日志（复用 Day 2 审计表）
2. 心跳：`ping/pong` 20s 周期，45s 无帧判死 → 清理会话状态
3. WS trace 注入：连接建立时从 auth 消息绑定 trace_id，全连接生命周期日志携带（补 TraceIdMiddleware 不覆盖 WS 的缺口）
4. 信令转发：`offer→answer`、`ice` 双向透传；`asr_start/asr_end` → 调 C1 事件流；`tts_text` 下行
5. 断线检测与状态清理（ASR 会话/TTS 在途任务/帧缓冲，不动 PeerConnection）

## 验收标准（真实应用实例，禁止 mock——攻击面测试约定）

- `uv run pytest tests/test_audio_ws_auth.py -q` 全绿：伪造 token → 4401 + 审计落库；合法 token → auth_ok；心跳超时踢线；300ms 超时关闭
- 手工验证：wscat/浏览器两客户端互发 offer/answer 正常透传
- **核对项（登记结论到任务卡变更记录）**：设备会话 token 临期是否有 refresh 通道——E2 是人账号口径，若无则过期重走 E4，此结论同步 A4

## 时间节点

09:30 前冻结 C2 → 12:00 前鉴权+心跳完成 → 15:00 前信令转发完成 → 14:30 起与 A3/A4 联调 → 17:00 收口提交。

## 联调对接点

- A3 消费 offer/answer/ice（SDP 建连主通道）
- A4 消费全部消息（客户端信令实现）
- A1 的 C1 事件流经本层转成 `tts_text`/partial/final 下行

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-05 | 初稿 |
| v1.1 | 2026-11-05 | 核对项结论登记：设备会话**无 refresh 通道**（已核实 `app/api/auth.py`——E2 `POST /api/sessions/refresh` 仅收人账号 refresh_token，设备凭证只有 E4 换证一个入口，24h 到期作废）。落地口径：A2 的 WS 层不做 token 续期，过期统一回 `auth_fail code=expired` 并 4401 关闭；客户端凭本机 device_code 重走 E4 换新 token 后重连。结论已同步 A4 卡（交付物 7 已按此精确化，见其 v1.2） |
| v1.2 | 2026-11-05 | **实现完成**。①C2 契约冻结：四条会裁决落盘 contracts.md v1.1（单向心跳 20s/45s 判死；重复 auth → invalid+4401；同 device 踢旧留新 4002；非法/未知消息 → error internal 不关连接）；②交付 `app/audio/signaling.py`（C2 TypedDict 原样拷贝、首帧鉴权复用 `resolve_principal`、审计只落结构化日志五字段——`audit_logs` 表 Day 9 建后回填、进程内注册表 + 踢旧留新加锁、单一 writer 协程、公开面仅 `deliver`/`deliver_asr_event`/`set_media_handler`）+ `app/main.py` 一行路由装配；③测试替代口径：弃用 Starlette TestClient（自带事件循环会跨循环使用测试事务），改**进程内 ASGI harness** 直驱 app——坑：必须先发 `{"type":"websocket.connect"}` 才能触发 `accept()`（Starlette accept 内部先 receive 等 connect 帧）；④坑：无界队列 `Queue.put()` 是协程，同步上下文不 await 会静默丢消息（RuntimeWarning 才暴露），全部改 `put_nowait`；⑤room_id 经 `device_service.list_elder_candidates` 只读复用（audio→services 单向，不 import ORM）；⑥验收：`pytest tests/test_audio_ws_auth.py -q` 15/15 通过，全量回归 285 passed，ruff check/format 通过。实施计划见 docs/plans/task-a2-信令WS与鉴权.md |
