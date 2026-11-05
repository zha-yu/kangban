> 状态：草稿　｜　版本：v1.1　｜　日期：2026-11-04
> 关联：docs/team_tasks/task-5-登录与凭证接口.md
> 上游依据：docs/接口契约_身份与权限.md（契约冻结）｜ docs/康伴3D数字人_PRD.md（AC-24、FR-ADM-03/04）｜ docs/康伴3D数字人_技术设计文档.md（§3.2 staff_accounts、§3.2 guardian_accounts、§3.2 devices、§5 凭证设计）
> 变更记录见文末

---

# 种子提示词（执行前先读本节，后续编码即按此喂 AI）

```
【Context】
康伴 = CCRC 长者卧室的 3D 数字人陪伴系统。本模块是身份认证核心链路，
位置：app/services/auth_service.py（唯一写库层） + app/api/auth.py（路由层） + tests/test_auth_api.py（测试）。
依赖的既有模块（必须复用，不许重写）：
  · app/core/security.py        — bcrypt 密码哈希、JWT 签发/校验、typ 三体系区分、空 secret 拒签（task-4 交付）
  · app/core/auth.py            — get_current_principal 依赖、require_roles（task-4 交付）
  · app/core/runtime_context.py — Identity 数据结构（kind + id + role）（task-4 交付）
  · app/core/config.py          — access_token_expire_minutes=120 / refresh_token_expire_days=7 / elder_session_expire_hours=24 / jwt_secret（已实现）
  · app/models/__init__.py      — StaffAccount / GuardianAccount / Device / ElderStaffAssignment / ElderGuardian ORM（task-3 交付）
  · app/core/context.py         — get_trace_id / TRACE_HEADER（已实现，日志脱敏用）
  · app/core/logging.py         — 结构化日志（已实现）

【Task】
只做四件事：
  1. app/services/auth_service.py — 领域服务（唯一写库层）：
     login_with_phone / refresh_access_token / exchange_device_token / get_current_identity
  2. app/api/schemas/auth.py — 8 个 Pydantic 请求/响应 DTO
  3. app/api/auth.py — 4 个端点：
     POST /api/sessions、POST /api/sessions/refresh、GET /api/sessions/current、POST /api/device-sessions
  4. tests/test_auth_api.py — 6 条验收全覆盖的测试用例

允许改动的文件：只有以上 4 个。禁止触碰：app/main.py（E 独占）、app/models/**、alembic/**（task-3 交付后只读）、app/core/security.py（B 的地盘，task-4 已交付）。

【Constraints】
- 技术栈：FastAPI + Pydantic v2 + SQLAlchemy 2.x(async) + PostgreSQL 15/pgvector；依赖按 pyproject.toml 已声明版本（pyjwt[crypto]、bcrypt 已在），**不新增依赖**
- 分层硬约束（AGENTS §2）：services 是唯一写库层 → auth.py **禁止** import ORM；auth.py 只做参数校验 + 调 service + 序列化 + 错误码映射
- 核心安全约束（AGENTS §4 + 契约 §3 + 契约 §5）：
  * token_version 每次校验都比对（AC-24）
  * 三套凭证体系（human_access / human_refresh / device / staff_card）typ 严格区分，禁止串用
  * 登录失败对外文案统一"用户名或密码错误"，**不区分**"账号不存在"与"密码错误"（防枚举）
  * 密码永远不回显；响应体不含 password_hash / jwt_secret / device_secret
  * 日志中 phone 脱敏（仅后 4 位）；每行必带 trace_id
  * 设备长期令牌用 sha256 比对（契约 S-03），不是明文比对
- 禁区（AGENTS §4）：
  * 不硬编码密钥；jwt_secret 为空时 SecurityManager 拒签（task-4 已实现）
  * 不连生产数据库；只连 docker compose 起的本地 postgres
  * 不新增第三种同类能力实现（auth 接口一套，不再造第二套）
  * 不改已合入的 Alembic 历史迁移

【形态】
直线（FastAPI 路由 + 服务函数），理由：
四问法全否 ——
  Q1 不涉及重试循环（登录/刷新/换会话都是单次请求，失败即抛异常，无"重试第 N 次"）；
  Q2 无互斥分支后的不同处置（staff vs guardian 是查询分派，不是业务分支）；
  Q3 无不可回滚副作用（登录签发 token 是纯读库 + 纯计算，refresh 同理）；
  Q4 无人工卡点（Day 8 之后的 admin 维护接口才涉及改 token_version，当前 task-5 只消费 token_version 不生产）。
→ 不引入 LangGraph。

【Expected Result】
  1. `uv run pytest tests/test_auth_api.py -q` → **6 passed**（全部验收覆盖）
  2. `uv run ruff check app tests` → **All checks passed**（无告警）
  3. `uv run pytest tests/test_security.py -q && uv run pytest tests/test_auth_api.py -q` → 两组都全绿（确认没破坏 task-4）
  4. 手工 curl 四条端点（见 S5.1）→ 各自返回契约 §4 规定的 JSON 结构 + 正确 HTTP 码
  5. 人工审 diff（AGENTS §5 auth 高风险文件清单）→ auth.py + auth_service.py 逐行检查 §5.2 审查清单全部勾完
  6. psql `\d staff_accounts` + `\d guardian_accounts` + `\d devices` → 三表字段与 TD line 349-322 一致（task-3 保证，task-5 回归）

【停止条件】
跑通上面 6 条验收即停止。不要顺手写：admin 维护接口（Day 8）、staff_tokens 工牌令牌接口（Day 9）、elder CRUD（task-6）、main.py 路由注册（task-7）。不要重命名任何既有文件。

若卡住：先汇报「卡在哪一步、依据是什么、建议两个方案」，等我选。汇报格式：
  卡点：<具体描述>
  依据：<哪份文档/契约/代码>
  方案 A：<怎么做> + 代价
  方案 B：<怎么做> + 代价
  建议选：<A 或 B>
```

---

# 以下是详细执行步骤（种子提示词的 Task 展开）

## 一、前置条件检查

task-5 依赖 task-3 和 task-4 全部完成后才能解锁。实际执行前必须确认：

| 依赖项 | 验收点 | 谁提供 |
| --- | --- | --- |
| `app/models/__init__.py` 导出 StaffAccount / GuardianAccount / Device ORM | `from app.models import StaffAccount` 不报错 | A（task-3） |
| `alembic upgrade head` 已建 staff_accounts / guardian_accounts / devices 表 | 三张表在 DB 中存在，字段与 TD line 349-322 一致 | A（task-3） |
| `app/core/security.py` 已实现密码哈希、JWT 签发/校验、typ 三体系区分、token_version 比对、空 secret 拒签 | 5 类失败用例全绿 | B（task-4） |
| `app/core/auth.py` 已实现 get_current_principal 依赖、require_roles、current_device | 能解 Bearer token 返回 Principal | B（task-4） |
| `app/core/runtime_context.py` 已实现 Identity 数据结构 | contextvar 注入可用 | B（task-4） |

**解锁前自检命令（拿到 task-3、task-4 交付物后先跑这个）：**

```bash
uv run pytest tests/test_security.py -q
uv run alembic upgrade head
uv run python -c "from app.models import StaffAccount, GuardianAccount, Device; print('OK')"
uv run python -c "from app.core.security import SecurityManager; print('OK')"
uv run python -c "from app.core.auth import get_current_principal; print('OK')"
```

---

## 二、任务拆解与子任务依赖图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        task-3 / task-4 已解锁                        │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌──────────────────────────────┐   ┌──────────────────────────────────────┐
│ S1. Pydantic 模型层（约 0.5h）│   │ S2. auth_service 领域服务（约 2.0h）  │
│     8 个请求/响应 DTO         │   │   唯一写库层                         │
│                               │   │   login_with_phone                  │
│ • LoginRequest / LoginResponse│   │   refresh_access_token              │
│ • RefreshRequest / RefreshRes │   │   exchange_device_token             │
│ • DeviceLoginRequest / Device │   │   get_current_identity              │
│ • CurrentIdentity / Error     │   │                                      │
└──────────────────┬────────────┘   └──────────────────────┬───────────────┘
                   │                                       │
                   │         可并行开发                     │
                   └───────────────┬───────────────────────┘
                                   │
                                   ▼
                   ┌────────────────────────────────────┐
                   │ S3. API 路由层 app/api/auth.py      │ 约 1.0h
                   │   POST /api/sessions               │
                   │   POST /api/sessions/refresh       │
                   │   GET  /api/sessions/current        │
                   │   POST /api/device-sessions        │
                   └──────────────────┬─────────────────┘
                                      │
                                      ▼
                   ┌────────────────────────────────────┐
                   │ S4. 测试层 tests/test_auth_api.py   │ 约 1.5h
                   │   6 条验收标准覆盖                  │
                   │   每条写操作回查 DB                 │
                   └──────────────────┬─────────────────┘
                                      │
                                      ▼
                   ┌────────────────────────────────────┐
                   │ S5. 集成验证 & 安全审查             │ 约 1.0h
                   │   手工 curl 冒烟 + AGENTS §5 审 diff│
                   └────────────────────────────────────┘
```

---

## 三、子任务详细说明

### S1. Pydantic 模型层（约 0.5 工时）

**新建文件：** `app/api/schemas/auth.py`

| 模型 | 字段 | 依据 |
| --- | --- | --- |
| `LoginRequest` | `phone: str`、`password: str` | 契约 E1 |
| `LoginResponse` | `access_token: str`、`refresh_token: str`、`token_type: str="bearer"`、`expires_in: int`、`principal: PrincipalInfo` | 契约 E1 |
| `RefreshRequest` | `refresh_token: str` | 契约 E2 |
| `RefreshResponse` | `access_token: str`、`expires_in: int` | 契约 E2 |
| `DeviceLoginRequest` | `device_id: str`、`device_secret: str` | 契约 E4 |
| `DeviceLoginResponse` | `access_token: str`、`expires_in: int`、`device: DeviceInfo` | 契约 E4 |
| `CurrentIdentityResponse` | `kind: str`、`id: str`、`role: str\|None`、`name: str`、`scope_elder_ids: list[str]` | 契约 E3 |
| `ErrorResponse` | `detail: str`、`code: str`、`trace_id: str` | 契约 §7 |

**注意：** `expires_in` 取 Settings 字段的计算值（`config.access_token_expire_minutes * 60`），不写字面量。

---

### S2. auth_service 领域服务（约 2.0 工时）

**新建文件：** `app/services/auth_service.py` — **唯一操作 ORM 的层**，`app/api/auth.py` 禁止 import ORM。

#### S2-1. `async def login_with_phone(phone: str, password: str) -> LoginResult`

```
1. 查 staff_accounts WHERE phone = :phone
   ├─ 找到 → SecurityManager.verify_password(password, staff.password_hash)
   │   ├─ 匹配 → 查 status='active'，用 SecurityManager 签发 human_access + human_refresh（tv=token_version）
   │   └─ 不匹配 → 抛 AuthError("用户名或密码错误")   ← 防枚举，对外统一文案
   └─ 没找到 → 查 guardian_accounts WHERE phone = :phone
       ├─ 找到 → 同上（role=None，kind="guardian"）
       └─ 没找到 → 抛 AuthError("用户名或密码错误")   ← 两表都没命中也是同一文案

2. 返回 LoginResult（principal 含 kind/id/role/name，不含任何密码相关字段）
```

#### S2-2. `async def refresh_access_token(refresh_token: str) -> RefreshResult`

```
1. SecurityManager.verify_jwt(refresh_token, expected_type="human_refresh")
2. 拆 sub="staff:<uuid>" 或 "guardian:<uuid>"，查对应表取 token_version
3. payload.tv != 库中 token_version → 抛 AuthError("token 已失效")   ← AC-24
4. status='disabled' → 抛 AuthError
5. 签发新 access token（tv=当前 token_version，typ="human_access"）
```

#### S2-3. `async def exchange_device_token(device_id: str, device_secret: str) -> DeviceLoginResult`

```
1. 查 devices WHERE device_id = :id
2. status='locked' → 抛 AuthError
3. sha256(device_secret) != devices.token_hash → 抛 AuthError   ← 契约 S-03 两层凭证
4. elder_id IS NULL → 查该房间 beds 是否有在住长者；无则抛 AuthError("设备未绑定长者")
5. 签发设备会话 JWT（typ="device"，tv=token_hash 前 16 位，exp=24h）
```

#### S2-4. `async def get_current_identity(principal: Identity) -> CurrentIdentityResult`

```
根据 principal.kind 分派：
  staff   → scope_elder_ids = elder_staff_assignments (staff_id + active) 的 elder_id 列表；admin 返回空列表（全量权限）
  guardian → scope_elder_ids = elder_guardians (guardian_id + active) 的 elder_id 列表
  device  → scope_elder_ids = 按 room_id 推导的 beds 中在住长者
```

**S2 完成前自测清单：**
- [ ] 密码哈希用 bcrypt，不存明文
- [ ] 日志中 phone 脱敏（仅后 4 位）
- [ ] 错误文案不区分"账号不存在"和"密码错误"
- [ ] 返回体不含 password_hash 或 jwt_secret
- [ ] token_version 每次校验都比对（AC-24）
- [ ] 不 import ORM 的是 auth.py，service 层才 import

---

### S3. API 路由层（约 1.0 工时）

**新建文件：** `app/api/auth.py` — 禁止 import ORM，只做三件事：

1. Pydantic 参数校验（依赖 S1 的模型）
2. 调 auth_service 方法（依赖 S2）
3. 序列化响应 + 异常 → 错误码映射

| 端点 | 方法 | 路径 | 鉴权 | 关键逻辑 |
| --- | --- | --- | --- | --- |
| E1 登录 | POST | `/api/sessions` | 无 | auth_service.login_with_phone |
| E2 刷新 | POST | `/api/sessions/refresh` | 无 | auth_service.refresh_access_token |
| E3 当前身份 | GET | `/api/sessions/current` | Bearer token | `get_current_principal` → auth_service.get_current_identity |
| E4 设备会话 | POST | `/api/device-sessions` | 无 | auth_service.exchange_device_token |

**错误码映射（契约 §7）：**

| service 层异常 | HTTP | `code` |
| --- | --- | --- |
| `AuthError("用户名或密码错误")` | 401 | `TOKEN_INVALID` |
| `AuthError("账号已停用")` | 401 | `TOKEN_REVOKED` |
| `AuthError("token 已过期")` | 401 | `TOKEN_EXPIRED` |
| `Pydantic ValidationError` | 422 | `VALIDATION_ERROR` |
| 其他未知异常 | 500 | `INTERNAL_ERROR` |

**不触碰 app/main.py（E 独占），task-7 统一注册路由。** 临时验证可用 `app.include_router(auth.router)` 在本地脚本里试。

---

### S4. 测试层（约 1.5 工时）

**新建文件：** `tests/test_auth_api.py` — **每条写操作必须"回查 DB"断言（契约 §10）**，只看 HTTP 200 视为无效。

#### 验收 1：错误密码 → 401

```python
async def test_login_wrong_password_returns_401() -> None:
    """错误密码统一返回 '用户名或密码错误'，对外不区分账号不存在 vs 密码错。"""
    # Given: 数据库里有 seed staff 账号
    # When: 用正确 phone + 错误 password
    response = await client.post("/api/sessions", json={"phone": TEST_PHONE, "password": "wrong"})
    # Then
    assert response.status_code == 401
    assert response.json()["code"] == "TOKEN_INVALID"
```

#### 验收 2：伪造/过期 token → 401

```python
async def test_fake_token_returns_401() -> None:
    response = await client.get("/api/sessions/current",
        headers={"Authorization": "Bearer fake.fake.fake"})
    assert response.status_code == 401

async def test_expired_token_returns_401() -> None:
    # SecurityManager 签发 1 秒过期 token
    expired_jwt = SecurityManager.create_access_token(
        sub=f"staff:{TEST_STAFF_ID}", kind="staff", role="admin",
        tv=0, expires_in_seconds=1,
    )
    await asyncio.sleep(2)
    response = await client.get("/api/sessions/current",
        headers={"Authorization": f"Bearer {expired_jwt}"})
    assert response.status_code == 401
    assert response.json()["code"] == "TOKEN_EXPIRED"
```

#### 验收 3：GET /api/sessions/current 解出角色

```python
async def test_current_identity_returns_role() -> None:
    login_resp = await client.post("/api/sessions", json=VALID_LOGIN)
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]

    resp = await client.get("/api/sessions/current",
        headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "staff"
    assert body["role"] in ("admin", "care_worker", "doctor")
    assert body["id"] and body["name"]  # 非空
```

#### 验收 4：设备 token 解出 device_id

```python
async def test_device_session_returns_device_id() -> None:
    resp = await client.post("/api/device-sessions", json={
        "device_id": str(TEST_DEVICE_ID),
        "device_secret": TEST_DEVICE_SECRET,   # 长期令牌明文
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["device"]["id"] == str(TEST_DEVICE_ID)

    # 用拿到的设备会话 token 再访问 /current
    cur = await client.get("/api/sessions/current",
        headers={"Authorization": f"Bearer {body['access_token']}"})
    assert cur.status_code == 200
    assert cur.json()["kind"] == "device"
    assert cur.json()["id"] == str(TEST_DEVICE_ID)
```

#### 验收 5：账号停用后旧 token → 401（AC-24）

```python
async def test_disabled_account_old_token_returns_401() -> None:
    login_resp = await client.post("/api/sessions", json=VALID_LOGIN)
    assert login_resp.status_code == 200
    old_token = login_resp.json()["access_token"]

    # 直接在 DB 里把 token_version +1（模拟 admin 禁用账号）
    await db.execute(
        update(StaffAccount).where(StaffAccount.staff_id == TEST_STAFF_ID)
        .values(token_version=StaffAccount.token_version + 1, status="disabled")
    )
    await db.commit()

    # Then: 旧 token → 401
    resp = await client.get("/api/sessions/current",
        headers={"Authorization": f"Bearer {old_token}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_REVOKED"
```

#### 验收 6：refresh 换新 access，旧 access 仍按 token_version 判定

```python
async def test_refresh_generates_new_access() -> None:
    login_resp = await client.post("/api/sessions", json=VALID_LOGIN)
    refresh_token = login_resp.json()["refresh_token"]
    old_access = login_resp.json()["access_token"]

    refresh_resp = await client.post("/api/sessions/refresh",
        json={"refresh_token": refresh_token})
    assert refresh_resp.status_code == 200
    new_access = refresh_resp.json()["access_token"]
    assert new_access != old_access

    # token_version 没变，新旧都能用
    for t in (old_access, new_access):
        r = await client.get("/api/sessions/current",
            headers={"Authorization": f"Bearer {t}"})
        assert r.status_code == 200

async def test_refresh_after_tv_invalidation_old_access_401() -> None:
    """refresh 后再 tv+1，旧 access → 401（AC-24 贯穿）"""
    login_resp = await client.post("/api/sessions", json=VALID_LOGIN)
    old_access = login_resp.json()["access_token"]

    # tv+1
    await db.execute(
        update(StaffAccount).where(StaffAccount.staff_id == TEST_STAFF_ID)
        .values(token_version=StaffAccount.token_version + 1)
    )
    await db.commit()

    resp = await client.get("/api/sessions/current",
        headers={"Authorization": f"Bearer {old_access}"})
    assert resp.status_code == 401
```

**测试辅助需求：** conftest 需要 seed 数据：至少 1 个 staff 账号（admin 或 care_worker）、1 个 guardian 账号、1 台 device（含长期令牌哈希 sha256 值）。如果 task-3 的 conftest 没有，task-5 在自己的测试里建 fixture。

---

### S5. 集成验证与安全审查（约 1.0 工时）

#### 5.1 手工冒烟测试

```bash
# 1. 人账号登录
curl -s -X POST http://localhost:8000/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"phone":"13800000001","password":"test123"}' | python -m json.tool
# 期望：200，含 access_token / refresh_token / principal

# 2. 当前身份（带上一步的 access_token）
curl -s http://localhost:8000/api/sessions/current \
  -H "Authorization: Bearer <access_token>" | python -m json.tool
# 期望：200，kind / id / role / name 非空

# 3. 刷新
curl -s -X POST http://localhost:8000/api/sessions/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token":"<refresh_token>"}' | python -m json.tool
# 期望：200，新的 access_token

# 4. 设备换会话
curl -s -X POST http://localhost:8000/api/device-sessions \
  -H "Content-Type: application/json" \
  -d '{"device_id":"<uuid>","device_secret":"<长期令牌明文>"}' | python -m json.tool
# 期望：200，含 device.id / device.room_id

# 5. 错误密码 → 401
curl -s -X POST http://localhost:8000/api/sessions \
  -d '{"phone":"13800000001","password":"wrong"}'
# 期望：401，{"detail":"用户名或密码错误","code":"TOKEN_INVALID",...}

# 6. 伪造 token → 401
curl -s http://localhost:8000/api/sessions/current \
  -H "Authorization: Bearer fake.fake.fake"
# 期望：401
```

#### 5.2 AGENTS §5 高风险文件人工审查清单

**`app/api/auth.py` 审查点：**
- [ ] 不 import ORM，不写业务逻辑
- [ ] 异常统一映射，不泄露内部细节
- [ ] 响应体不含 password_hash / jwt_secret / device_secret
- [ ] 错误文案防枚举（登录失败不区分账号不存在 vs 密码错误）
- [ ] 日志中 phone 脱敏（仅后 4 位），每行必带 trace_id
- [ ] 从 auth.py grep `StaffAccount\|GuardianAccount\|Device\|password_hash` → 零命中

**`app/services/auth_service.py` 审查点：**
- [ ] 是唯一操作 ORM 的层（auth.py 绝不碰）
- [ ] token_version 每次校验都比对（AC-24）
- [ ] 空密码哈希拒登录（不是 bcrypt("") 静默过）
- [ ] JWT 的 typ 严格校验，不串用三体系
- [ ] refresh 必须带 tv 比对（不是只验签名）
- [ ] 从 service grep `jwt_secret\|password_hash` → 零命中（只从 Settings 读）

---

## 四、工时估算汇总

| 子任务 | 预估工时 | 可并行 | 前置 |
| --- | --- | --- | --- |
| S1. Pydantic 模型层 | 0.5h | 是 | task-4 的 Identity 结构 |
| S2. auth_service 领域服务 | 2.0h | 是（与 S1 并行） | task-3 的 ORM、task-4 的 SecurityManager |
| S3. API 路由层 | 1.0h | 否 | S1 + S2 完成 |
| S4. 测试层 | 1.5h | 否 | S3 路由可调用 + conftest seed 就绪 |
| S5. 集成验证 & 安全审查 | 1.0h | 否 | S4 测试全绿 |
| **合计** | **6.0h** | | |

排期建议：task-3 和 task-4 同日完成 → task-5 当天下午或次日上午开始，1 个工作日内完成。

---

## 五、关键里程碑

| 里程碑 | 判定条件 | 预计 |
| --- | --- | --- |
| **M0：解锁** | task-3 ORM 就绪 + task-4 SecurityManager 就绪 | Day 2 上午 |
| **M1：service 层可用** | auth_service 四个方法独立调用不报错 | M0 + 2.5h |
| **M2：路由层冒烟** | curl 四端点都通，返回正确 JSON + 状态码 | M1 + 1.0h |
| **M3：测试全绿** | `uv run pytest tests/test_auth_api.py -q` → 6 passed | M2 + 1.5h |
| **M4：审 diff 通过** | S5.2 审查清单全部勾完 | M3 + 1.0h |
| **M5：合并** | PR 合入，task-5 卡面状态 ✅ | M4 通过 |

---

## 六、风险预判与应对

| # | 风险 | 概率 | 应对 |
| --- | --- | --- | --- |
| R1 | task-3 或 task-4 延期 | 中 | 提前写 S1 的模型 + S4 的测试骨架（mock SecurityManager）；找 A 要 ORM fixture 格式 |
| R2 | bcrypt 72 字节上限没处理 | 低 | auth_service 加防御：`len(password.encode()) > 72` 时先 sha256 预哈希；同时报给 B |
| R3 | 两表都命中同一手机号（唯一索引缺失） | 低 | service 层显式检测：两表都查到同一 phone → 抛业务异常 + ERROR 日志 |
| R4 | token_version 机制被跳过（refresh 只验签名） | 中 | ① 每个方法里显式写 tv 比对；② 验收 5 专测 tv+1 后旧 token 401；③ 审 diff grep `tv` |
| R5 | response 泄露 password_hash | 低 | service 不把 ORM 对象直接返回；用显式 Pydantic model 做白名单；审 diff grep `password_hash` |
| R6 | 登录失败文案没统一（枚举漏洞） | 中 | ① staff 两表都没命中 + 密码错，统一抛同一 AuthError；② 审 diff grep `"不存在"` 看有没有漏 |

---

## 七、验收标准详表（含可执行命令）

| # | 验收点 | 测试用例 | 命令 | 期望输出 |
| --- | --- | --- | --- | --- |
| **1** | 错误密码 → 401 | `test_login_wrong_password_returns_401` | `uv run pytest tests/test_auth_api.py::test_login_wrong_password_returns_401 -v` | 1 passed |
| **2a** | 伪造 token → 401 | `test_fake_token_returns_401` | `uv run pytest tests/test_auth_api.py::test_fake_token_returns_401 -v` | 1 passed |
| **2b** | 过期 token → 401 | `test_expired_token_returns_401` | `uv run pytest tests/test_auth_api.py::test_expired_token_returns_401 -v` | 1 passed |
| **3** | GET /current 解出角色 | `test_current_identity_returns_role` | `uv run pytest tests/test_auth_api.py::test_current_identity_returns_role -v` | 1 passed |
| **4** | 设备 token 解出 device_id | `test_device_session_returns_device_id` | `uv run pytest tests/test_auth_api.py::test_device_session_returns_device_id -v` | 1 passed |
| **5** | 账号停用后旧 token → 401（AC-24） | `test_disabled_account_old_token_returns_401` | `uv run pytest tests/test_auth_api.py::test_disabled_account_old_token_returns_401 -v` | 1 passed |
| **6a** | refresh 换新 access | `test_refresh_generates_new_access` | `uv run pytest tests/test_auth_api.py::test_refresh_generates_new_access -v` | 1 passed |
| **6b** | refresh 后 tv+1 旧 access → 401 | `test_refresh_after_tv_invalidation_old_access_401` | `uv run pytest tests/test_auth_api.py::test_refresh_after_tv_invalidation_old_access_401 -v` | 1 passed |
| **全量** | 6 条全覆盖 | — | `uv run pytest tests/test_auth_api.py -q` | **6 passed** |
| **安全** | 无 lint | — | `uv run ruff check app tests` | **All checks passed** |
| **回归** | 不破坏 task-4 | — | `uv run pytest tests/test_security.py -q && uv run pytest tests/test_auth_api.py -q` | 两组全绿 |
| **手工** | curl 四端点 | — | 见 S5.1 | 各自返回正确 JSON + HTTP 码 |
| **审查** | 人审 diff | — | 逐行读 auth.py + auth_service.py | S5.2 清单全部勾完 |

---

## 八、完成报告模板

```
## task-5 完成报告

### 一、diff 摘要
- app/api/schemas/auth.py: 8 个 Pydantic DTO
- app/services/auth_service.py: 4 个 async 方法 + AuthError
- app/api/auth.py: 4 个端点
- tests/test_auth_api.py: 8 条测试用例（覆盖 6 条验收）

### 二、6 条验收各自的断言位置
| # | 验收点 | 测试用例 | 关键 assert 行 |
|---|--------|----------|----------------|
| 1 | 错误密码 401 | ... | L.. |
| 2a | 伪造 token 401 | ... | L.. |
| 2b | 过期 token 401 | ... | L.. |
| 3 | /current 解出角色 | ... | L.. |
| 4 | 设备 token 解 device_id | ... | L.. |
| 5 | 停用后旧 token 401 (AC-24) | ... | L.. |
| 6a | refresh 换新 access | ... | L.. |
| 6b | tv+1 旧 access 401 | ... | L.. |

### 三、风险处理
| # | 是否发生 | 应对 |
|---|----------|------|
| R1 | 是/否 | ... |

### 四、剩余 TODO（如有）
- Day 8 admin 维护接口（POST /api/staff、PATCH /api/staff/{id}）
- Day 9 staff_tokens 工牌令牌接口（POST /api/staff-cards）
```

---

## 变更记录

| 版本 | 日期 | 变更 | 触发 |
| --- | --- | --- | --- |
| v1.0 | 2026-11-04 | 首次创建 | Day 2 规划 |
| v1.1 | 2026-11-04 | 对齐 seed-prompt.md 六要素模板：新增种子提示词块（Context/Task/Constraints/形态/Expected Result/停止条件）、补形态判定（直线）、补每条验收的可执行命令、集中 Constraints（复用清单 + 禁区 + 安全硬约束）、新增停止条件 | 自查发现未读 seed-prompt.md |
