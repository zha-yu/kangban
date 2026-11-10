> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-10
> 关联：docs/team_tasks/day06/contracts.md v1.8（同提交登记）｜mcp-servers/weather（骨架模板）
> 上游依据：PRD FR-ALT-01/02、FR-RPT-03、AC-38（验收）｜技术设计文档 §1.1 副作用收敛、§3 work_orders/notifications 表（实现）
> 变更记录见文末

# mock-operations MCP 提前实现（Day 8 范围、Day 7 空闲产能消化）

## 0. 背景与边界

`day06/contracts.md` v1.1 冻结口径「mock-operations 留 Day 8」。本计划把 **Day 8 里不依赖
任何 Day 7 在改文件的部分**提前消化：server 本体 + 写库服务 + 单测；**ToolExecutor 注册
接线（L7-6 写锁在 dev-C 手里）与 AC-39 越权端到端证据仍留 Day 8**，范围不扩大。

## 1. 交付物

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `app/services/notify_service.py` | 新增 | 家属（监护人）通知唯一写库口：按 `elder_guardians` active 绑定解析监护人、逐人落 `notifications`（`is_mock=true`、`status='generated'`）、`dedupe_key` 部分唯一幂等（并发走 begin_nested 回查） |
| `mcp-servers/mock-operations/operations_core.py` | 新增 | 两个工具的核心逻辑：入参强校验 → 失败四分类结构化返回（永不抛异常）；委托 `work_order_service` / `notify_service`，自身不写 SQL |
| `mcp-servers/mock-operations/server.py` | 新增 | MCPServer 骨架（照 weather）：stdio 默认 / `--http` 绑 `127.0.0.1:7102`，端口可用 `MOCK_OPERATIONS_MCP_PORT` 覆盖 |
| `tests/test_mcp_mock_operations.py` | 新增 | 参数校验零 DB 用例 + 真库落库/幂等/not_found 用例（savepoint 事务回滚隔离） |

## 2. 关键设计决策（实现口径，供 Day 8 接线对照）

1. **身份注入参数名 `subject_elder_id`**：`MCPClient._resolve_args_identity` 会把 args 里的
   `elder_id` 键丢弃（闸门③），MCP server 进程拿不到它——工具参数因此用
   `subject_elder_id`，**由 Day 8 的 ToolExecutor 包装层从 `runtime.elder_id` 注入**，
   模型传入无效。这是本计划最重要的接线约定。
2. **失败结构沿用 weather 约定**：`{"ok": false, "error": <机器码>, "message": <长者可读话术>}`；
   机器码 `bad_request`（参数错→bad_args） / `not_found`（目标不存在→如实告知） /
   `conflict` 与 `transient`（临时故障→transient）。`mcp_client.tool_error_class()` 零改动可映射。
3. **建单不指定 assignee**：MCP 工具不暴露 `assignee_id`（谁处理是护工的事，不是模型的），
   建单留「未分派」真实状态；`severity` 建单快照语义沿用 `work_order_service`。
4. **家属通知按绑定解析目标**：工具只收 `subject_elder_id`，监护人列表由
   `elder_guardians(status='active')` 解析（模型不得指定通知对象）；无绑定 →
   `not_found` 如实失败。逐监护人的幂等键为 `<dedupe_key>:<guardian_id>`。
5. **通知类型开放枚举校验**：`notifications.type` 无 CHECK（ER §5.3 口径），工具层用
   `^[a-z][a-z_]{1,63}$` 形态校验防注入，不设死白名单。
6. **payload 纪律**：必须 JSON 对象、可序列化、≤4096 字节（防塞库）；脱敏由调用方
   payload 构造时保证（与 `safety_service` 同一口径）。
7. **不新增 Settings / 迁移 / .env 项**：表全在（0010/0011），channel 复用
   `settings.notify_channel`，端口覆盖走环境变量豁免口径（同 weather）。

## 3. 验收命令

```bash
uv run pytest tests/test_mcp_mock_operations.py -q        # 单测（需本地 PG）
uv run pytest tests/test_mcp_mock_operations.py \
  --cov=app.services.notify_service --cov=operations_core \
  --cov-report=term-missing -q                            # 覆盖率（口径见下）
uv run ruff check app/services/notify_service.py mcp-servers/mock-operations tests/test_mcp_mock_operations.py
uv run python mcp-servers/mock-operations/server.py --http   # 起进程，curl 调 /mcp 验证两工具
```

### 覆盖率口径（FR-TC-06「核心 Tool 100%」对照，2026-11-10 实测 29 用例）

- `operations_core.py`：**99%**（仅剩 `event_id` 传入且建单成功的一条部分分支；
  失败四分类映射 conflict/bad_request/transient/not_found 已全部覆盖）；
- `notify_service.py`：82%，**两条豁免**（评审留痕，不补测）：
  1. `148-169` 并发抢插兜底（`begin_nested` 撞唯一索引后回查复用）——需要真并发才触发，
     逐人幂等路径已有独立用例覆盖，此条是竞态防御层；
  2. `69-70` payload 序列化炸分支——`json.dumps(default=str)` 已把几乎所有对象转为字符串，
     仅 `__str__` 也抛异常的对象可达，属不可达防御分支。

## 4. Day 8 待办（本计划不覆盖）

- `tool_executor.py` / `tool_registry.py` 注册两工具（等 dev-C C3 收口，L7-6）；
- 包装层从 `runtime.elder_id` 注入 `subject_elder_id`；
- AC-39 越权端到端用例证据（伪造 elder_id → forbidden + 审计）；
- 家属端推送状态展示（FR-RPT-03 前端，P8 family 后台）。

## 修订记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版：Day 8 mock-operations 中不依赖 Day 7 写锁的提前实现计划 |
| v1.1 | 2026-11-10 | 补 5 个失败映射用例（event_id not_found / WorkOrderConflict→conflict / WorkOrderError→bad_request / 两工具裸异常→transient 不泄内部细节），`operations_core.py` 覆盖率 87%→99%；§3 登记覆盖率命令与 `notify_service.py` 两条豁免口径（并发兜底 / 不可达序列化分支） |
