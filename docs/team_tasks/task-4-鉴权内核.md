# task-4 · Step2 鉴权内核（security / 依赖注入 / runtime_context）

| 项 | 内容 |
| --- | --- |
| 状态     | 🚧 进行中（T1–T3、T4a 已提交 `16513da`；T4b-1 人员/工牌接线待提交；T4b-2 设备回查等 `Device` 模型） |
| 认领人    | B                                                                    |
| 主责     | B（鉴权内核）                                                                |
| 前置     | `task-1`                                                             |
| 写作用域   | `app/core/security.py`、`app/core/auth.py`、`app/core/runtime_context.py`、`tests/test_security.py` |

> 进展（2026-11-04）：判定层（密码、四类 JWT、`Identity` 与 contextvar、库里事实的逐项比对）已提交 `16513da`；
> T4b-1 完成**人员与工牌**接线：`resolve_principal`（一类令牌只查一张表、查询键取已验签载荷）、
> 生成器依赖 `get_current_principal`（`try/finally` 注入与回滚）、`current_staff` / `current_guardian` /
> `current_device` / `require_roles`。用例 183 条（假会话 + 真实 `TestClient` 链路 + 9 条真库 AC-24 闭环）、
> 全量 190 条 0 skip，ruff check/format + mypy 全绿；本轮口径登记在契约 §8 **S-24 ~ S-26**。
> 三处已知边界：① `get_current_principal` 的**设备**分支要等 `app/models/device.py`（任务板 P-02），
> 现在对设备令牌抛 `DeviceLookupNotWiredError` 而不是伪装 401；② 契约 §7 的响应体与 `WWW-Authenticate`
> 落在 `app/core/errors.py` / `app/api/**`，不在本卡写作用域内，由 `task-5` 映射；
> ③ 工牌身份的 `elder_id`（读自设备绑定）随 ① 一起做（契约 S-22 已登记未做项）。
> 另登记 **P-03**：conftest 的 `db_conn` 与函数级事件循环冲突（跨循环复用 asyncpg 连接），
> 表现为数据库用例「一半通过一半 skip」；本卡先在 `test_security.py` 内自建 NullPool 的 `auth_db` 绕过，
> A 修好 conftest 后应删掉它改回 `db_conn`。

## 为什么能并行

纯函数阶段不碰数据库，可与 `task-3` 同时起步；等 A 的 ORM 就绪后再接线（`token_version` 要从库里读）。

## 改哪些文件

- `app/core/security.py`：bcrypt 密码哈希（注意 72 字节上限，超长先截断或用 sha256 预哈希）；JWT 签发/校验；三体系 `typ` 区分（`human_access` / `human_refresh` / `device` / `staff_card`）
- `app/core/auth.py`：`get_current_principal`（解析 Bearer）+ `require_roles(*roles)` + `current_staff` / `current_guardian` / `current_device` 依赖
- `app/core/runtime_context.py`：`Identity` 数据结构（`kind` + `staff_id`/`guardian_id`/`device_id`/`elder_id` + `role`），contextvar 注入
- `tests/test_security.py`

## 硬约束（AGENTS §5 auth 审查点）

1. **`jwt_secret` 只从 Settings 读；为空时必须拒绝签发/校验并报错** —— `app/core/config.py:46` 默认就是空串，直接签等于空密钥签发。
2. **过期值取 Settings 字段**，不许写字面量：Access 2h / Refresh 7d / 设备会话 24h。
3. **三套凭证体系不得串用**：人 token 不能当设备 token 用（校验 `typ`）。
4. **`token_version` 失效**：每次校验都要比对载荷里的 `ver` 与库里的 `token_version`（FR-ADM-04 / AC-24）。
5. **`runtime_context` 只接受服务端构造**，绝不接受模型或客户端传入的 `elder_id` / `thread_id`（AGENTS §4.9）。

## 验收命令

```bash
uv run pytest tests/test_security.py -q
```

必须覆盖五类失败：伪造签名、过期、`typ` 混用、`ver` 失配、空 secret 拒签 —— 全部 401 或抛明确异常。

## 禁止触碰

`app/api/**`、`app/models/**`、`alembic/**`、`app/main.py`。依赖不用新增：`pyjwt[crypto]`、`bcrypt` 已在 `pyproject.toml:44-45`。

## 风险

`app/core/security.py`、`app/core/auth.py` 属 AGENTS §5 高风险（auth），**合并前必须人审 diff**。

## 报告

diff 摘要 + 五类失败用例各自的断言点。
