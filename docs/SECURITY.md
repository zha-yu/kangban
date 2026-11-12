# 安全设计说明（SECURITY.md）

> 状态：已冻结　｜　版本：v1.0　｜　日期：2026-11-12
> 上游依据：PRD FR-ADM-06（三道隔离闸门）/ AC-18 / AC-24；技术设计文档 §1.3、§5；Day 9 任务书 A2
> 变更记录见文末

---

## 1. 三道隔离闸门（PRD FR-ADM-06 ①②③）

请求侧身份自外向内收敛为三个互不越权的数据边界：

| 闸门 | 边界 | 唯一实现 | 关键机制 |
| --- | --- | --- | --- |
| ① 会话隔离 | 同一设备同一时刻只属于一个长者会话 | `app/services/identity_service.py`（生成：`_open_thread`，identity_service.py:121-168）＋ `app/core/runtime_context.py`（格式层：`parse_elder_thread_id` 等） | thread_id 按 `(elder_id, session_seq)` **服务端拼接**（identity_service.py:143），前端不可指定；形状校验零 I/O 先行，DB 归属校验 `validate_thread_belongs_to_device`（identity_service.py:382）为权威裁决；generic（未绑定）会话用 uuid4().hex 且不允许声称属于长者 |
| ② 记忆隔离 | 长期记忆召回强制 `WHERE elder_id`，跨长者向量检索拒绝 | `app/services/memory_service.py`（Day 9 dev-B2 加固中，本文件登记口径） | 召回失败 → 本轮不加载记忆，不得臆造（AGENTS §6）；embedding 未接入期间用 difflib 文本相似兜底（B2 修复中） |
| ③ 工具身份 | 模型/客户端传入的 `elder_id` 一律丢弃，身份只从 runtime_context 注入 | `app/services/tool_executor.py`（tool_executor.py:49-57 `IDENTITY_KEYS` 消毒） | `require_identity()` 无身份抛 `IdentityNotSetError`；自报身份 → 丢弃 ＋ `tool_identity_override_attempt` 审计 ＋ runtime 注入（AC-39①） |

依赖方向硬约束：`core` 不 import `services`（分层单向），因此闸门①的 DB 级裁决在
identity_service 而非 runtime_context —— core 层只提供「构造即合法」的格式与身份判定。

## 2. 攻击矩阵（已覆盖用例 → tests/test_gates.py）

| 攻击 | 入口 | 防线 | 结果 | 用例 |
| --- | --- | --- | --- | --- |
| 伪造 thread_id 形状（客户端随手编 / generic 冒充长者会话） | 任何未来携带 thread 维度的入口 | `parse_elder_thread_id` 格式层 | `ThreadFormatError`，零 I/O | `test_gate1_format_layer_rejects_forged_shapes` |
| 跨设备拿别人 thread（形状合法） | 同上 | `validate_thread_belongs_to_device`（DB 权威裁决） | `False` → 调用方映射 403 | `test_gate1_db_layer_ownership` |
| thread elder 维度与身份不符 | 同上 | `thread_matches_device_identity`（纯代码预判） | `False`（staff/guardian 无 elder 维度一律 False） | `test_gate1_pure_identity_match_rejects_cross_elder_and_staff` |
| 无身份调用工具 / dict 冒充身份 | 工具层 | `require_identity` / `set_identity` isinstance 硬拦 | `IdentityNotSetError` / `IdentityConstructionError` | `test_gate3_no_identity_raises_identity_not_set` |
| 模型自报 `elder_id=B` | 工具入参 | `tool_executor` 身份消毒 | 丢弃 ＋ DB 审计 ＋ runtime 注入 | `test_gate3_model_supplied_elder_id_dropped_and_audited` |
| 护工读不在管长者详情 | `GET /api/elders/{id}` | 范围授权（elder_service，先判范围再查库） | 403 `SCOPE_FORBIDDEN` ＋ 日志审计（AC-18） | `test_ac18_care_worker_out_of_scope_detail_403_and_audited` |
| 家属读未绑定长者详情 | 同上 | 家属范围 = 绑定长者（`elder_guardians`） | 403 ＋ 日志审计 | `test_ac18_guardian_out_of_scope_detail_403` |
| 员工停用后旧令牌重放 | 任意 staff 接口 | `token_version` 硬比对（auth.py:214） | 401 `TOKEN_REVOKED`（AC-24） | `test_ac24_staff_disable_invalidates_old_token` |
| 设备解绑后旧令牌重放 | 任意 device 接口 | `devices.token_hash` 前 16 位轮换判定（auth.py:292） | 401 `TOKEN_REVOKED`（AC-24） | `test_ac24_device_unbind_invalidates_old_token` |
| 伪造 JWT / 篡改签名 / refresh 冒充 access | 任意接口 | 验签 + `typ` 分派 | 401 `TOKEN_INVALID` | tests/test_auth_attacks.py（Day 8 既有） |
| 跨设备声音克隆冒充 | WS 建连 | 声纹比对 cosine ≥ 0.62 ＋ 长者主动确认 | 降级 generic 会话 | identity_service（Day 8 交付） |

## 3. 缓解措施清单（按防线归属）

1. **服务端生成一切身份锚点**：`elder_id` / `thread_id` / `turn_id` / `trace_id` 只由服务端产生；客户端与模型传入的同名字段一律丢弃（AGENTS §4.9、FR-ADM-06 ③）。`Identity` 构造即校验（kind 与字段一一对应），`dataclasses.replace` 也塞不进伪造字段。
2. **先判范围再查库**：范围外主体拿到的永远是 403，不泄露存在性（elder_service.py:8）。
3. **审计两级形态**（既有口径，A-02）：写操作与工具越权落 `audit_logs` DB 行（`record_audit`，trace_id 自动入 detail）；范围外**读**落结构化日志（`audit_action/actor/target/result/trace_id`），供日志平台检索串联。两级都带 trace_id。
4. **凭证撤销即时生效**：员工停用/改角色 → `token_version += 1`；设备解绑 → `token_hash` 轮换。旧令牌在下一次请求即 401，不等过期。
5. **脱敏集中**：唯一实现 `app/core/redact.py`（Day 9 A1b）；logging formatter 出口统一脱敏，`trace_id` 在白名单绝不脱敏；日志样本三条 PII/密钥 grep 零命中（验收留痕见计划执行记录）。
6. **密钥不入代码**：JWT secret / API key / TURN credential 全部经 Pydantic Settings 从 `.env` 读取；测试用 monkeypatch 显式注入，不读本地 `.env`。
7. **审计不含敏感值**：`record_audit` 与 `_audit` 只放 ID、字段名与判定结果；姓名/手机号/token 禁入审计（audit_log_service.py:10）。

## 4. 测试入口

```bash
# 三道闸门攻击用例（本文件第 2 节矩阵，AC-18/24/闸门①③）
uv run pytest tests/test_gates.py -q

# Day 8 攻击套件（伪造凭证/越权建档/设备重放/WS 攻击面）
uv run pytest tests/test_auth_attacks.py tests/test_audio_security.py -q

# 全量门禁（pre-push 同款）
uv run python tests/eval_gate.py
```

测试原则：攻击用例不 mock 鉴权依赖与 service（mock 掉任何一层都会让「拦下来了」变成「假拦」）；
数据库用外层事务 ＋ savepoint 回滚隔离，连不上库显式 skip 不静默通过。

## 5. 已知缺口与登记（不静默）

| # | 缺口 | 现状 | 影响 / 后续 |
| --- | --- | --- | --- |
| G1 | 闸门①「403 ＋ DB 审计」入口级闭环 | REST/WS 现状**零入口**接受客户端 thread_id；`validate_thread_belongs_to_device` 无业务调用方（仅单测） | 防线已备（格式层＋DB 层＋纯代码层三层齐）；等 X1 回执帧（dev-B/D）等携带 thread/task 维度的入口落地时一行接线，届时本文件补入口级用例 |
| G2 | AC-18 范围外**读**的审计落 `audit_logs` DB 行 | 现状为结构化日志审计（A-02 口径），DB 行仅用于写操作 | 若主理人裁决「读也要 DB 行」，需改 elder_service._audit（非 dev-A 写域，需交接） |
| G3 | 闸门②记忆隔离攻击用例 | memory_service 加固由 dev-B2 进行中（D9 16:00 前） | B2 落地后 M5.1 阶段补 `test_gates.py` 闸门②用例并回填本文件 |

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-12 | 初版（Day 9 A2 交付）：三道闸门口径、攻击矩阵 11 行、缓解措施 7 条、测试入口、缺口登记 G1–G3。 |
