# task-5 · Step3 登录与凭证接口

| 项 | 内容 |
| --- | --- |
| 状态 | ✅ 已合入 |
| 认领人 | — |
| 主责 | C（登录接口） |
| 前置 | `task-3`（ORM）、`task-4`（依赖注入） |
| 写作用域 | `app/api/schemas/auth.py`（DTO）、`app/services/auth_service.py`（唯一写库层）、`app/api/auth.py`（路由）、`tests/test_auth_api.py`（集成测试）、`app/main.py`（router 注册 + 全局 exception handler） |

## 改哪些文件

- `app/services/auth_service.py`（**唯一写库层**：查账号、验密、读 `token_version`、停用校验）
- `app/api/auth.py`（路由只做参数校验 + 调 service + 序列化）
- `tests/test_auth_api.py`

## 必须实现

端点以 `docs/接口契约_身份与权限.md`（`task-1` 产出）为准（**契约优先，不沿用大纲里的 `/auth/*` 路径**）：

- `POST /api/sessions` — 人账号登录（staff / guardian 共用）
- `POST /api/sessions/refresh` — 刷新 Access Token
- `GET /api/sessions/current` — 当前身份详情（需 Bearer）
- `POST /api/device-sessions` — 设备长期令牌换 24h 会话

**建模提醒**：人账号是两张表 —— 员工走 `staff_accounts`（`role ∈ admin/care_worker/doctor`），家属走 `guardian_accounts`（技术设计文档 line 349/361）。家属**不是** role 枚举值，别写一张 users 表。

## 验收（对齐 AC-24）

| # | 验收 | 测试用例 | 状态 |
|---|------|---------|------|
| 1 | 错误密码 → 401 TOKEN_INVALID | `test_login_wrong_password_returns_401` | ✅ |
| 1a | 不存在手机号 → 同一 401 文案（防枚举） | `test_login_nonexistent_phone_returns_same_error` | ✅ |
| 2 | 伪造 token → 401 | `test_fake_token_returns_401` | ✅ |
| 2a | 过期 token → 401 TOKEN_EXPIRED | `test_expired_token_returns_401` | ✅ |
| 3 | `GET /api/sessions/current` 解出 staff / guardian 角色 + scope_elder_ids | `test_current_identity_staff` / `test_current_identity_guardian` | ✅ |
| 4 | 设备 token 解出 device_id + elder_id | `test_device_session_success` | ✅ |
| 4a | 设备错 secret → 401 | `test_device_session_wrong_secret_401` | ✅ |
| 5 | 账号停用 / `token_version+1` 后旧 token → 401 TOKEN_REVOKED | `test_disabled_account_old_token_401` + `test_tv_invalidation_kills_old_access_and_refresh` | ✅ |
| 6 | `refresh` 换出新 access + tv 失效后旧 access / 旧 refresh 都 401 | `test_refresh_generates_new_access` | ✅ |

## 硬约束

- `app/api/` 不写业务逻辑、不直接操作 ORM（AGENTS §2）
- 密码永远不回显；响应体不含 `password_hash` / `jwt_secret`
- 日志里 `phone` 必须脱敏，且每行带 `trace_id`
- 登录失败对外文案不区分「账号不存在」与「密码错误」（防枚举），内部日志可区分

## 禁止触碰

`app/models/**`、`alembic/**`、`app/core/security.py`（B 的地盘）。

**例外**：`app/main.py` 在本次交付里做了 router 注册 + 全局 exception handler（task-7 职责，但 auth router 必须注册才能跑通端点验证；已与用户确认先合入）。

## 报告

**diff 摘要**（`git diff --stat`）：
```
 app/api/auth.py                          |  210 +++++++++++++++++++++
 app/api/schemas/__init__.py              |    2 +
 app/api/schemas/auth.py                  |  102 +++++++++++++++
 app/services/auth_service.py             |  379 ++++++++++++++++++++++++++++++++++++++++++++++
 app/main.py                              |  +110（router 注册 + 9 个全局 exception handler）
 tests/test_auth_api.py                   |  438 ++++++++++++++++++++++++++++++++++++++++++++++
```

**测试门禁**：
- `pytest tests/test_auth_api.py -q` → **11 passed**
- `pytest tests/test_security.py -q`（task-4 回归）→ **193 passed**
- 合计 **204 passed, 0 failed, 0 skipped**
- `ruff check app/api/ app/services/auth_service.py app/main.py tests/test_auth_api.py` → **All checks passed**

**运行时验证**：
- `POST /api/sessions` → 200 access+refresh（staff / guardian 共用）
- `GET /api/sessions/current` → 200 kind+role+scope_elder_ids
- `POST /api/sessions/refresh` → 200 新 access
- `POST /api/device-sessions` → 200 device{id, room_id, elder_id}
- 所有错误路径 → 统一 `{detail, code, trace_id}` JSON 响应
