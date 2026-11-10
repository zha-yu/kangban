> 状态：已完成　｜　版本：v1.0　｜　日期：2026-11-10
> 关联：docs/team_tasks/day07/dev-d-day07.md（D2 §3.2-2）｜docs/team_tasks/day07/README.md §5.7（冻结契约）｜docs/plans/day07/handoff-B-to-D-打断契约与终态口径.md（D1 侧）
> 上游依据：adhoc-20261110-day6全面审查.md P2-19/20、P3-33/34｜迁移 0014（metrics CHECK）/0015（elders.updated_at，经主理人裁决授权）
> 交付方向：dev-B（B3 metrics/E20/API 边界，已收口）→ dev-D（D2 console 改绑分页与错误 UX）

# 交接件 · E20 改绑错误码契约与并发口径（B3 → D2）

> 这份是 B 给 D 的**正式交接件**（D2 侧）。错误码与字段以本文为准；
> 服务端已于 Day 7 Step 6 实现并回归绿，D2 可按本文做 401/400/409/422 文案与差集提交。

## 0. 一句话结论

**D2 按 §5.7 差集口径提交即可；409 有两种语义**（`CONFLICT` = 失去最后责任护工，
`STALE_WRITE` = 页面加载后被人改过），**422 有两类**（schema 校验 / 参数越界），
文案映射见 §3。

## 1. 接口契约（PUT /api/staff/{staff_id}/elders，admin only）

```text
PUT /api/staff/{staff_id}/elders
body   {
         "elder_ids": ["uuid", ...],          # 全量覆盖语义；前端只提交差集；上限 200
         "expected_updated_at": "ISO8601?"    # 可选（P2-20 乐观并发，见 §2）
       }
200    { "staff_id", "added": ["uuid"], "removed": ["uuid"] }
403    { code: "ROLE_FORBIDDEN" | "PROTECTED_ADMIN" }
404    { code: "NOT_FOUND" }
409    { code: "CONFLICT" | "STALE_WRITE" }
422    FastAPI 标准校验错误体
```

- `elder_ids` 上限 **200**（P3-34）：第 201 个起整体 422，不截断执行；
- `GET /api/staff` 的 `role`/`status` query 参数改为 Literal 校验（P3-33）：
  `role=superman` → **422**，不再是 200 空列表——前端过滤下拉只需三值
  `admin|care_worker|doctor`，`status` 两值 `active|disabled`。

## 2. 两种 409 的语义与前端处理

| code | 触发条件 | 前端处理 |
| --- | --- | --- |
| `CONFLICT` | 移除后某长者将失去全部 primary_care（P2-19 行锁保证并发下也只有一个提交者能成功） | 文案"该长者将失去最后一位责任护工"，**保留当前编辑态**（D2 卡 §3.2-2 原文） |
| `STALE_WRITE` | 请求带了 `expected_updated_at`，且涉改动长者的 `updated_at` 比基准新——页面加载后别人改过（P2-20） | 文案"绑定已被他人修改，请刷新后重试"，**自动刷新列表**，不保留旧编辑态 |

**并发保证**：两个 admin 同时摘同一位长者的不同护工，行锁串行化 → 恰好 1 成功、
1 个 `CONFLICT` 409；长者永远至少保留 1 位责任护工。

## 3. `expected_updated_at` 接入（可选，分两步走）

1. **现在可不做**：不传 → 服务端只走行锁（行锁已保证"1 成功 1 409"）；
2. **接入方式**（D2 后续迭代）：GET 员工列表暂未返回各长者 `updated_at`——接入前
   需要一个读口径（登记项，见 §5）；拿到后提交时带上页面加载时刻的基准即可。

## 4. admin 自我保护（403 PROTECTED_ADMIN）

- admin **不能停用自己**（PATCH 自己 `status=disabled` → 403）；
- 不能停用**最后一位** active admin；
- 前端 AdminUsersPage：当前登录 admin 是唯一 active admin 时，停用按钮应置灰
  （服务端已兜底，前端只是体验）。

## 5. 登记项（不在本日范围）

- GET /api/staff 暴露各绑定长者的 `updated_at`（D2 接入 `expected_updated_at` 的前置）；
- 分页完整拉取长者候选由 D2 按 §5.7 实现（后端 elders 列表接口已有分页能力）。

## 6. 服务端问题回滚路径

改绑/错误码异常回 **B3**，附 `trace_id` + 请求/响应原文 + `audit_logs` 中
`action='reassign_elders_rejected'` 的行；**不在前端兜底**（不吞错误码、不伪造成功）。

## 7. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | B3 收口（Step 6）正式交接：E20 错误码契约、两种 409 语义、CAS 可选接入、admin 自我保护 |
