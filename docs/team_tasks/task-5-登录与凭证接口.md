# task-5 · Step3 登录与凭证接口

| 项 | 内容 |
| --- | --- |
| 状态 | 🔒 锁定（待 `task-3`、`task-4`） |
| 认领人 | — |
| 主责 | C（登录接口） |
| 前置 | `task-3`（ORM）、`task-4`（依赖注入） |
| 写作用域 | `app/services/auth_service.py`、`app/api/auth.py`、`tests/test_auth_api.py` |

## 改哪些文件

- `app/services/auth_service.py`（**唯一写库层**：查账号、验密、读 `token_version`、停用校验）
- `app/api/auth.py`（路由只做参数校验 + 调 service + 序列化）
- `tests/test_auth_api.py`

## 必须实现

端点以 `docs/接口契约_身份与权限.md`（`task-1` 产出）为准：

- `POST /auth/login`
- `POST /auth/refresh`
- `GET /auth/me`
- `POST /auth/device/register`

**建模提醒**：人账号是两张表 —— 员工走 `staff_accounts`（`role ∈ admin/care_worker/doctor`），家属走 `guardian_accounts`（技术设计文档 line 349/361）。家属**不是** role 枚举值，别写一张 users 表。

## 验收（对齐 AC-24）

1. 错误密码 → 401
2. 伪造/过期 token → 401
3. `GET /auth/me` 能解出当前角色
4. 设备 token 能解出 `device_id`
5. 账号停用（`status=disabled` 或 `token_version+1`）后旧 token → 401
6. `refresh` 能换出新 access，且旧 access 仍按 `token_version` 判定

## 硬约束

- `app/api/` 不写业务逻辑、不直接操作 ORM（AGENTS §2）
- 密码永远不回显；响应体不含 `password_hash` / `jwt_secret`
- 日志里 `phone` 必须脱敏，且每行带 `trace_id`
- 登录失败对外文案不区分「账号不存在」与「密码错误」（防枚举），内部日志可区分

## 禁止触碰

`app/models/**`、`alembic/**`、`app/core/security.py`（B 的地盘）、`app/main.py`（E 独占）。

## 风险

`app/api/auth*.py` 在 AGENTS §5 高风险清单里 —— 合并前人审 diff。

## 报告

diff 摘要 + 上述 6 条验收各自的断言位置。
