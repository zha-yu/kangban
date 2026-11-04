# task-04 · 鉴权内核实施计划

| 项      | 内容                                                                                               |
| ------ | ------------------------------------------------------------------------------------------------ |
| 状态     | 进行中（T1、T2、T3、T4a、T4b-1、T4b-2、T5 已完成；剩 T6 人工审查交付）                                                 |
| 对应任务   | `docs/team_tasks/task-4-鉴权内核.md`                                                                 |
| 主责     | B（鉴权内核）                                                                                          |
| 前置     | `task-1` 已完成；纯函数先行，数据库接线等待 `task-3`                                                              |
| 实现写作用域 | `app/core/security.py`、`app/core/auth.py`、`app/core/runtime_context.py`、`tests/test_security.py` |

## 1. 任务目标

交付可供登录接口、业务路由及后续 Agent 工具复用的鉴权内核：

1. 提供安全的密码哈希与校验能力，覆盖 bcrypt 72 字节上限及 Unicode 密码。
2. 按冻结契约签发、解析人员 Access/Refresh、设备会话和工牌受限四类 JWT，严格隔离凭证类型。
3. 在每次受保护请求中回查数据库，验证账号/设备/工牌状态及令牌版本，支撑 AC-24 即时失效。
4. 提供 FastAPI 身份与角色依赖，并通过 `contextvars` 注入服务端构造的 `Identity`。
5. 用自动化测试覆盖正常链路、五类必测失败及上下文隔离；合并前完成 auth 高风险文件人工审查。

对应需求：PRD §3.1、FR-ADM-03/04/06/08、AC-24/39；技术设计 §1.3 `app/core`、§6.2；冻结契约 §5、§7、§10。

## 2. 需求依据与统一口径

### 2.1 已阅读依据

- `docs/康伴3D数字人_PRD.md`：角色权限、2h/7d/24h 有效期、即时失效、身份强制注入。
- `docs/康伴3D数字人_技术设计文档.md`：`app/core` 职责、三闸门、账号与设备字段、工牌表结构。
- `docs/接口契约_身份与权限.md`：四类载荷、校验顺序、401/403 边界及验收映射。
- `docs/team_tasks/README.md`、`task-1`、`task-3`、`task-4`、`task-5`、`task-7`：并行关系、文件所有权和交付边界。
- `app/core/config.py`、`app/core/context.py`、`app/core/trace.py`、`pyproject.toml`：现有 Settings、contextvar 模式、依赖与测试约束。

### 2.2 冲突裁决

任务卡与已冻结契约存在命名差异。按任务板“冲突以契约为准”的规则，采用下表，不在代码中兼容两套载荷：

| 事项          | 本任务采用                            | 不采用                              |
| ----------- | -------------------------------- | -------------------------------- |
| 人员令牌 `typ`  | `access` / `refresh`             | `human_access` / `human_refresh` |
| 版本载荷名       | `tv`                             | `ver`                            |
| 设备/工牌 `typ` | `device` / `staff_card`          | 其他别名                             |
| 签名算法        | Settings 中的 `HS256`，解码时显式限定算法白名单 | 从 JWT header 动态选择算法              |

工牌接口属于 Day 9，本任务只实现其底层签发/校验能力。Settings 尚无工牌 TTL 字段，因此工牌签发函数必须接收调用方给出的明确有效期，不在本任务新增配置字段或写死时长。

## 3. 范围与边界

### 3.1 本任务实现

- `security.py`：密码哈希、JWT 签发/解码、凭证类型与必需载荷校验、明确的鉴权异常。
- `runtime_context.py`：`Identity` 数据结构、身份 contextvar 的设置/读取/重置。
- `auth.py`：Bearer 解析、按类型回查数据库、构造身份、角色及主体依赖。
- `test_security.py`：纯函数单元测试、依赖测试和 `task-3` 就绪后的数据库接线测试。

### 3.2 明确不做

- 不实现登录、刷新、设备换会话等 API；由 `task-5` 调用本任务能力。
- 不实现长者范围过滤、绑定关系鉴权、声纹识别或 `thread_id` 生成；由后续任务负责。
- 不实现工牌兑换接口；`staff_card` 仅完成内核能力。
- 不修改 `app/api/**`、`app/models/**`、`alembic/**`、`app/main.py`，不新增依赖。
- 不从请求参数、模型输出或工具参数接收 `elder_id` / `thread_id`，不提供绕过服务端身份的公共入口。
- 本卡不越权增加全局异常处理器；契约错误响应体的统一映射留待允许修改错误装配的任务处理。

## 4. 子任务拆解

| 编号    | 子任务            | 主要产出                                                                                                                                                   | 依赖                                                                 |
| ----- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------ |
| T1    | 密码安全原语         | `hash_password`、`verify_password`，统一预哈希策略                                                                                                              | 无数据库                                                               |
| T2    | JWT 安全原语       | 四类令牌签发/解码、Settings 校验、载荷校验                                                                                                                             | 无数据库                                                               |
| T3    | 运行时身份上下文       | 不可变 `Identity`、contextvar 生命周期管理                                                                                                                       | 无数据库                                                               |
| T4a   | 鉴权纯判定层         | Bearer 解析、三类主体解码、库里事实比对、身份构造、角色判定（可测、不碰库）                                                                                                              | 无数据库                                                               |
| T4b-1 | 人员与工牌接线（✅ 已完成） | `resolve_principal`（一类令牌只查一张表）、`get_current_principal` 生成器依赖、`current_staff` / `current_guardian` / `current_device` / `require_roles`                 | `task-3` 的 `app/db.py` 与 `staff` / `guardian` / `staff_tokens` ORM |
| T4b-2 | 设备会话回查（✅ 已完成）  | `_load_device_row` 读 `devices.device_id / token_hash / status / elder_id` 并送进 T4a 判定；工牌分支补 `devices` JOIN 带出绑定长者；原 `DeviceLookupNotWiredError` 随缺口补齐删除 | `task-3` 的 `Device` ORM（`f2fb1a0`，任务板 P-02 已闭环）                    |
| T5    | 分层测试（✅ 已完成）    | 覆盖收口：解码侧一致性核对（真密钥手工签矛盾载荷）、三条兜底路径、家属缺行；四层结构与未覆盖行清单落档                                                                                                    | T1–T4                                                              |
| T6    | 审查与交付报告        | diff 摘要、五类失败断言点、人审记录（底稿：`docs/plans/adhoc-20261104-鉴权内核审查底稿.md`）                                                                                       | 全部实现完成                                                             |

## 5. 实现步骤

### 5.1 T1：密码哈希与校验

1. 使用现有 `bcrypt` 依赖，不引入 passlib。
2. 采用统一的 **SHA-256 预哈希 + bcrypt** 流程，哈希和校验必须走同一规范化路径；不静默截断密码。
3. 预哈希结果编码为稳定 ASCII 文本后再交给 bcrypt，避免原始 Unicode 字节长度及 bcrypt 72 字节上限造成差异。
4. `verify_password` 对错误密码返回 `False`，对格式非法或损坏的哈希返回 `False` 或明确的内部错误，不泄露哈希细节。
5. 测试覆盖普通密码、错误密码、超过 72 字节的密码、中文/emoji 密码及不同密码不可互相验证。

### 5.2 T2：JWT 签发与纯解码

1. 从注入的 Settings 或 `get_settings()` 读取 `jwt_secret`、`jwt_algorithm` 和三类已配置 TTL；禁止散读环境变量及写入字面量 TTL。
2. 在签发和解码入口共同检查密钥：空串或纯空白必须抛明确配置异常，不得签发空密钥令牌，也不得继续校验。
3. 使用 UTC aware datetime 生成 `iat` / `exp`；解码时要求 `sub`、`typ`、`iat`、`exp`，显式限定算法并验证签名和过期时间。
4. 解码 API 必须接收 `expected_typ`，缺少该参数或不匹配均拒绝；`refresh` 不能作为 API Access 使用，人/设备/工牌不可互用。
5. 按下表构造载荷，并校验 `kind` 与 `sub` 前缀、设备 `dev` 与 `sub`、工牌 `scope` 类型等内部一致性：

| 类型         | 关键载荷                                                                           | TTL 来源                        |
| ---------- | ------------------------------------------------------------------------------ | ----------------------------- |
| 人员 Access  | `sub=staff:<id>` 或 `guardian:<id>`、`kind`、员工 `role`、整数 `tv`、`typ=access`       | `access_token_expire_minutes` |
| 人员 Refresh | 同主体字段、`tv`、`typ=refresh`                                                       | `refresh_token_expire_days`   |
| 设备会话       | `sub=device:<id>`、`kind=device`、`dev`、当前 `token_hash` 前 16 位 `tv`、`typ=device` | `elder_session_expire_hours`  |
| 工牌受限       | `sub=staff:<id>`、`kind=staff_card`、`dev`、`scope`、`typ=staff_card`              | 调用方显式传入                       |

1. 对伪造签名、过期、错误算法、缺少必需字段、非法主体格式分别给出可区分但对客户端不泄密的失败原因；日志不得记录原始 token 或密钥。

### 5.3 T3：`Identity` 与 contextvar

1. 定义冻结、可 slots 化的 `Identity`，至少包含 `kind`、`staff_id`、`guardian_id`、`device_id`、`elder_id`、`role`；工牌受限范围用不可变 `scope` 元组表达。
2. 仅保留当前主体相关 ID，其余为 `None`；员工角色以数据库当前值为准，家属不是员工 role。
   | <br /> | <br /> |
   | ------ | ------ |
   | <br /> | <br /> |
3. 使用 `ContextVar[Identity | None]`，默认 `None`，提供设置、读取和按 token 重置能力；读取无身份时抛明确异常。
4. 身份只由 `auth.py` 在验签及数据库校验成功后构造。`elder_id` 只能来自服务端设备绑定或后续身份识别结果，不能从客户端/模型输入复制。
5. FastAPI 依赖采用 `try/finally` 重置 contextvar，确保请求结束或异常后不向下一请求泄露身份。

> 落地口径（T3 已完成）：字段矩阵与失败语义登记为契约 **S-17 / S-18**，实现见
> `app/core/runtime_context.py`；`canonical_uuid`、`STAFF_ROLES`、`normalize_scope`
> 从 `security.py` 提为公开并在身份构造里复用，**不再写第二份校验**。

### 5.4 T4：数据库回查与依赖注入

`task-3` 就绪后接入 `get_session` 和对应 ORM。`get_current_principal` 按以下固定顺序执行：

1. 解析 `Authorization: Bearer <token>`；缺失、格式错误、空 token 一律 401，并带 `WWW-Authenticate: Bearer`。
2. 解码并仅允许 `access` / `device` / `staff_card` 进入请求主体解析；`refresh` 一律 401。
3. 按 `typ` 分派到对应表，禁止跨表“尝试匹配”：
   - 人员 Access：按 `kind + sub` 查询 `staff_accounts` 或 `guardian_accounts`，要求记录存在、`status=active`、数据库 `token_version == tv`；员工同时核对数据库 role 与令牌 role，最终以数据库 role 构造身份。
   - 设备会话：查询 `devices`，要求设备存在且非 `locked`，当前 `token_hash` 前 16 位与 `tv` 相等，并核对 `device_id == dev`；绑定的 `elder_id` 从数据库记录读取。
   - 工牌受限：以原始 token 的 SHA-256 哈希查询 `staff_tokens.token_hash`，要求 `expires_at > now`，核对 staff/device 与载荷一致，并校验员工有效且符合工牌主体约束。
4. 任一记录缺失、状态无效、版本/哈希失配或过期均返回 401；数据库异常不得被误写成“凭证有效”。
5. 构造 `Identity` 并注入 runtime context，再返回给路由。
6. 提供 `current_staff`、`current_guardian`、`current_device` 依赖，主体种类不符返回 401；提供 `require_roles(*roles)`，在有效员工主体上做角色检查，角色不足返回 403。
7. `require_roles` 只负责角色，不承担长者范围过滤；范围授权仍由服务层按绑定表完成。
8. 为 `task-5` 保留清晰的 Refresh 解码入口；刷新流程中的账号状态和 `tv` 回查由 `task-5` 服务层复用同一判定规则，避免刷新接口绕过失效检查。

> 落地口径（T4a 已完成，纯判定层）：上面 1–3、6–8 条的**判定规则**已在 `app/core/auth.py`
> 实现并测试；第 3 条里「查询哪张表、怎么拿 session」属 T4b，**判定不许在依赖层重写一遍**。
> T4a 新决策登记为契约 **S-19 \~ S-23**：S-19 分派不读未验签 `typ`、S-20 角色取库、
> S-21 设备只 `locked` 拒、S-22 工牌归属/状态/到期四条 + **故意不比对** **`staff_tokens.scope`**、
> S-23 Bearer 头语法。
> 其中 S-22 的未做项是显式留白：R9 的 `scope` 是单个 text，载荷是列表，切分口径在 PRD/TD
> 均无出处，等 Day 9 的 E14 定完口径再补比对。
>
> 落地口径（T4b-1 已完成，人员与工牌接线）：第 3 条的三张表里 `staff_accounts` /
> `guardian_accounts` / `staff_tokens` 已接 `get_session` 做**只读 SELECT**（判定与查询分离：
> `_load_*_row` 只把行交回 `*_identity_from_*`，规则不在依赖层重跑），第 5、6 条的注入/回滚与
> 三个主体依赖、`require_roles` 全部落地。本轮新决策登记为契约 **S-24（主体种类不符 → 401）/
> S-25（查不到行 →** **`TOKEN_REVOKED`，不回 404）/ S-26（缺头必须由内核出 401，参数须带** **`= None`
> 默认值，否则落到框架 422）**。
> 第 1 条的 `WWW-Authenticate: Bearer` **本卡不做**：它是响应头，落点在异常处理器
> （`app/api/**` 或 `app/core/errors.py`），不在 task-4 写作用域内；本卡只保证抛出的异常类型与
> 契约 §7 的 `code` 正确，`task-5` 据此映射状态并补头。第 4 条的「设备」分支见 T4b-2。
>
> 落地口径（T4b-2 已完成，设备与工牌 `elder_id` 接线）：第 3 条的设备分支按上面写的口径落地——
> `_load_device_row` 只读 `device_id / token_hash / status / elder_id` 四列，行交回
> `device_identity_from_session`（S-21 的判定一条没重跑）；工牌分支再加一处 `devices` JOIN，
> 把绑定长者带进 `staff_card_identity_from_card`，S-22 未做的只剩 `scope` 比对。
> 上一轮的 `DeviceLookupNotWiredError` 已**删除**（缺口补齐即不留兼容壳，AGENTS §8）。
> 本轮不新增自裁：判定口径 T4a 就定完了，契约只补 S-22/S-25 的接线事实（§12 v1.6）。

### 5.5 T5：测试实施顺序

1. **无数据库阶段**：先完成密码、JWT、runtime context 测试，保证可与 `task-3` 并行。
2. **数据库接线阶段**：复用 `task-3` 的 PostgreSQL 事务夹具构造员工、家属、设备和工牌记录，验证真实回查与即时失效。
3. 无数据库环境只能按 `conftest` 明确 skip 并输出原因；正式合并验收必须在有库环境执行，不能把 skip 计作通过。
4. 依赖测试可在 `test_security.py` 内定义最小 FastAPI 测试路由，不修改 `app/api/**` 或 `app/main.py`。
5. 过期用例通过显式构造过去时间的 `exp`，不新增时间冻结依赖。

> 落地口径（T5 已完成）：五步按顺序全部落地，本卡最终形态是**四层结构**——① 纯函数（无库无网络）、
> ② 假会话（只验「查了哪张表、绑了什么值」）、③ `TestClient` 走完整 ASGI 依赖链（含 contextvar 回滚）、
> ④ 真实 PostgreSQL（只读四张表，事务内插入、结束回滚，连不上按 `conftest` 口径 skip 并输出原因）。
> 第 3 条的 skip 口径已实现：`auth_db` 夹具对任何连接异常 `pytest.skip(f"PostgreSQL 不可达…{exc!r}")`，
> 本轮实测 0 skip。第 4 条的最小路由 `_probe_client` 内含 `/whoami`、`/device-session` 等端点，
> 未修改 `app/api/**` 或 `app/main.py`。
> **实质收口不是凑覆盖率，是「删掉任何一条判定都有用例变红」**：T5 之前 `security.py` 86%，
> 未覆盖的正是 `_validate_payload` 的**解码侧**那半边——注释号称「签发前一遍、验签后一遍」，
> 但签发入口自己就会拒掉矛盾载荷，所以删掉 `decode_token` 里那次调用，旧用例一条都不会红。
> 矛盾载荷只能用**真密钥手工签**（`_signed_payload`）构造，现实中来自旧版本签出的令牌与被改过的白名单。
> 另补三条兜底（底层库意外异常 → 401、异常文案含密钥 → 就地吞掉、白名单加新 `typ` 却没有分派分支 → 401 且不查库）
> 与两处真实缺口（家属缺行、身份缺 `principal_id`）。判定规则一条没动，本轮不新增自裁。

## 6. 关键风险与应对

| 风险                       | 影响                             | 应对                                                              |
| ------------------------ | ------------------------------ | --------------------------------------------------------------- |
| 任务卡旧命名与冻结契约不一致           | 下游签发的 token 无法互通，或误兼容扩大攻击面     | 以契约 `typ=access/refresh/device/staff_card`、`tv` 为唯一口径，并在代码审查中核对 |
| 只验 JWT、不回查数据库            | 停用、改角色、解绑后旧 token 仍可用，违反 AC-24 | 每次主体解析都查询当前记录；对人员、设备、工牌分别写失效用例                                  |
| `task-3` 尚未完成导致阻塞        | ORM、会话依赖和测试夹具无法接线              | 先交付纯函数与测试；数据库阶段只读其公开模型/会话接口，不代改其文件                              |
| bcrypt 72 字节与 Unicode 差异 | 长密码报错或哈希行为不一致                  | 所有密码统一 SHA-256 预哈希；补充长字节和多语言测试                                  |
| contextvar 未清理           | 串请求身份泄露，形成严重越权                 | `try/finally` 重置；连续请求与异常路径测试                                    |
| 401 与 403 混用             | 无法区分凭证失效和权限不足，契约验收失败           | 凭证/主体错误 401，已认证但角色不足 403，范围授权留给服务层                              |
| 工牌 TTL 未配置               | 实现者可能写死短时效                     | 内核要求调用方显式传入；Day 9 再确定接口层 TTL 配置                                 |
| 错误响应体尚未全局映射              | 仅状态码正确但响应结构不完全符合契约 §7          | 本卡保证 401/403 与明确异常；由错误装配任务统一响应体，不越权修改 `main.py`                 |
| 鉴权属于高风险文件                | AI 生成缺陷可能直接影响全部受保护资源           | 合并前人工逐行审查 `security.py`、`auth.py` diff，并复核测试确有有效断言              |

## 7. 验收标准

### 7.1 功能验收

- [x] 密码哈希/校验支持超过 72 字节及 Unicode 密码，错误密码不能通过。
- [x] Access、Refresh、设备、工牌令牌均可按契约签发和解码，TTL 来自 Settings 或工牌显式参数。
- [x] 空 `jwt_secret` 在签发和校验两侧均明确失败。
- [x] 五类必测失败全部有独立用例和有效断言：伪造签名、过期、`typ` 混用、`tv` 失配、空 secret 拒签，结果为 401 或明确异常。
- [x] 人员账号 `disabled`、`token_version+1` 后旧 Access/Refresh 均不能通过相应校验。Access 侧已由真库用例闭环（`test_db_token_version_bump_revokes_the_issued_token`、`test_db_disable_and_role_edit_revoke_without_touching_the_token`）；**Refresh 的状态/`tv`** **回查按 §5.4 第 8 条留给** **`task-5`** 服务层复用同一判定函数，本卡只提供 `decode_refresh_token`。
- [x] 设备哈希轮换/解绑后旧设备会话 401；工牌记录不存在或过期 401。两侧均已闭环：设备走 `_load_device_row`（真库用例 `test_db_device_unbinding_rotates_the_hash_and_revokes`、`test_db_device_lock_and_absent_row_revoke`，假会话另有 `locked` / 哈希轮换两条），工牌有记录不存在、过期、员工停用三条真库用例。`offline` 与 `elder_id IS NULL` 两条**不拒**的状态也各有用例，防止把心跳问题判成凭证作废。
- [x] 人员 token 不能调用设备依赖，Refresh 不能充当 Access，设备 token 不能通过员工角色检查。
- [x] 有效员工但角色不足时 `require_roles` 返回 403；主体种类不符返回 401（契约 S-24，并有真实 `TestClient` 用例证明工牌先撞 401 那道门）。
- [x] `Identity` 仅在校验成功后进入 context，请求结束及异常后均恢复为空；无任何客户端/模型 `elder_id` 注入入口。
- [x] 日志和测试失败输出不包含原始密码、完整 token 或 `jwt_secret`。

### 7.2 工程验收

```bash
uv run pytest tests/test_security.py -q
uv run pytest -q
uv run ruff check app tests
```

- [x] 上述命令在有 PostgreSQL 测试库的环境通过；数据库用例无静默 skip。（2026-11-04 本机 docker：`pytest -q` 227 条全绿、0 skip；13 条真库用例（`test_db_`，T5 把 `test_db_absent_staff_row_revokes` 扩成员工/家属各查一次的 `test_db_absent_account_row_revokes`，条数不变）走本文件的 `auth_db` 夹具，隔离契约与 conftest 的 `db_conn` 相同——事务 + 回滚 + 连不上即 skip。**注意**：`auth_db` 自建 NullPool 引擎是为了绕开 `db_conn` 的跨事件循环连接池缺陷（任务板 P-03），A 修好 conftest 后应删掉它、改回 `db_conn`。）
- [x] 三个鉴权模块的行覆盖收口（T5）。命令 `uv run pytest -q --cov=app.core.security --cov=app.core.auth --cov=app.core.runtime_context --cov-report=term-missing` → `security.py` **100%**、`auth.py` 99%、`runtime_context.py` 99%，合计 **99.45%**（T5 前分别 86% / 96% / 97%，合计 91.59%）。剩余 2 行**故意不补并写明理由**：`auth.py:88` 是无人传入的 `AuthError(code=…)` 死参数、`runtime_context.py:210` 是被 `__post_init__` 与 `frozen/slots` 双重堵死的兜底。清单落档在测试文件末尾「T5 交付证据 · 未覆盖行清单」；`code=` 那条留给 T6 人审决定删或留，不用测试把它合法化。
- [x] 仅修改本任务允许的四个实现/测试文件，不新增依赖，不触碰其他任务写作用域。（`auth.py` 按卡面对 `app/db.py::get_session` 与 `app/models/**` 做**只读 SELECT**，四张表：`staff_accounts` / `guardian_accounts` / `devices` / `staff_tokens`，不写库、不碰迁移；`tests/test_security.py` 里 `rooms` / `devices` / `elders` 的夹具写入全部改走 ORM 的 `__table__.insert()`，P-02 交付的 `Device` 模型已用上。）
- [x] `security.py`、`auth.py` 已人工逐行审查 diff。
- [ ] 交付报告包含 diff 摘要，以及五类失败用例各自的测试名和断言点。

## 8. 交付与后续衔接

1. 向 `task-5` 说明密码、四类 JWT、Refresh 解码和身份依赖的公开函数及异常语义。
2. 向 `task-6` 说明 `current_staff` / `current_guardian` / `current_device` 和 `require_roles` 的使用方式；范围过滤不在本内核内实现。
3. 向 `task-7` 提供伪造签名、过期、类型混用、版本失配和设备解绑用例位置，供攻击测试与全链路验收复用。
4. 若实现中发现契约未覆盖的新决策，先登记并请求裁决，不在代码中静默引入新的载荷名、有效期或权限语义。

***

## 9. 变更记录

| 版本   | 日期         | 变更                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---- | ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| v1.0 | 2026-11-04 | 首版计划：T1–T6 拆解、实现步骤、风险与验收标准                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| v1.1 | 2026-11-04 | T1 完成：`security.py` 密码哈希/校验 + `tests/test_security.py`；bcrypt cost 与 NFKC 预哈希登记为契约 S-11/S-12                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| v1.2 | 2026-11-04 | T2 完成：四类令牌签发 + 纯解码（不含回查库）落地，88 条单测、全量 95 条、ruff check/format、mypy 全绿。**事实纠正**：原计划与代码注释假设「pyjwt 的 `leeway` 不作用于 `iat`」，pyjwt 2.15.1 实测**推翻**——`leeway` 对 `iat` 有效，但它是全局旋钮、会连带放宽 `exp`。实现做法不变（关 `verify_iat` + 自判 ≤60s），理由换成实测成立的那条，注释与用例已同步（`test_pyjwt_leeway_is_the_wrong_knob_for_iat_skew`）。**测试自纠**：`STAFF_ID`/`GUARDIAN_ID`/`DEVICE_ID` 三个常量全是数字，`.upper()` 是空操作，导致两条「大写 UUID 规范化」用例静默退化为空测试；改用一个含字母的 `LETTERED_ID` 并在用例内断言 `upper() != 原值`，防止再次无声失效。新增契约自裁 S-13/S-14/S-15/S-16（见契约 §12 v1.2）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| v1.3 | 2026-11-04 | T3 完成：`app/core/runtime_context.py` 落地 `Identity`（frozen + slots，`__post_init__` 强制字段矩阵）与 contextvar 载体（`set/get/require/reset/identity_scope/as_mapping`），新增 25 条用例，单文件 113 条、全量 120 条、ruff check/format、mypy（`security.py` + `runtime_context.py`）全绿。**复用而非重造**：把 `security.py` 的 `_canonical_uuid` / `_STAFF_ROLES` / `_normalize_scope` 提为公开 `canonical_uuid` / `STAFF_ROLES` / `normalize_scope`，身份构造直接调用——令牌与身份必须同一套判定。**实现自查**：`identity_scope` 初版漏了 `@contextmanager`，返回裸 generator，被 T3 用例当场抓到（`TypeError: 'generator' object does not support the context manager protocol`）——「异常必回滚」这条不写用例就根本测不出来。**登记**：新增契约自裁 S-17（身份字段矩阵，`staff`/`guardian` 不容纳 `elder_id`）、S-18（注入只收 `Identity` 实例、无身份抛 `IdentityNotSetError`、写入必须成对回滚）。**回报但不擅改**：`uv run mypy app/core` 在 Day 1 的 `app/core/context.py:44`、`app/core/trace.py:45` 各报一条 `union-attr`（`bool(value) and` 之后 mypy 不收窄），不在 task-4 写作用域内，等主理人决定是否顺手修。                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| v1.4 | 2026-11-04 | T4 拆成 T4a（纯判定层，本轮完成）/ T4b（`get_session` 接线，等 task-3）。T4a 落地 `app/core/auth.py`：`bearer_token_from_header`、`decode_principal_token`、`decode_refresh_token`、三类 `*_identity_from_*` 构造函数、`device_hash_prefix_matches`、`staff_card_lookup_key`、`check_roles`，异常系 `AuthError` 五种子类各带契约 §7 的 `code`；新增约 40 条用例，单文件 145 条、全量 152 条、ruff check/format、mypy（三个 auth 文件）全绿。**判定与接线分离**：`auth.py` 只吃「从库里读出的事实」（`db_role` / `db_status` / `db_token_hash` / `db_expires_at` 等关键字参数），SQL 与 session 留在 T4b，避免同一条规则在依赖层重写第二份。**两处口径纠正**：`decode_principal_token` 的三类尝试循环原先注释写「先判 typ 再验签」，实为逐类完整验签、过期在 typ 核对之前命中——注释按实现改正；员工角色越界的用例前提写错（漂移判定先命中，永远走不到构造），改成显式断言「库里脏角色进不了身份」。**lint 自纠**：4 个契约 §7 响应码常量里 3 个被 ruff `S105` 误判成硬编码口令，加 `# noqa: S105` 并在上一行写清「是响应码字面量，不是密钥」，不放宽全局规则。**登记**：新增契约自裁 S-19 \~ S-23（见契约 §12 v1.4），其中 S-22 明确记录工牌本轮**不比对** `staff_tokens.scope` 及原因。                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| v1.5 | 2026-11-04 | T4b-1（人员与工牌接线）完成：`app/core/auth.py` 新增 `resolve_principal`、生成器依赖 `get_current_principal`、`_load_staff_row` / `_load_guardian_row` / `_load_staff_token_row` 三条只读 SELECT、`current_staff` / `current_guardian` / `current_device` / `require_roles`，以及 `DeviceLookupNotWiredError`（设备分支未接线，**刻意不伪装成 401**）。新增 34 条用例（假会话 + `TestClient` 真实 ASGI 链 + 9 条真库），单文件 183 条、全量 190 条、0 skip，ruff check/format、mypy（三个 auth 文件）全绿。**两条只有走真实链路才暴露的问题**：① 只写 `Annotated[str \| None, Header()]` 时 FastAPI 把 `Authorization` 当必填头，缺头直接 422 `{"type":"missing"}`，`bearer_token_from_header` 根本不被调用——与 §7「凭证缺失 = 401」冲突，改成 `= None`（有默认值只能排末位，故 `get_current_principal` 的参数顺序与 `resolve_principal` 相反）；② 工牌主体直接调 `require_roles(...)(identity)` 会得到 403，而真实链路先经 `current_staff` 得 401——顺序只在框架里成立，因此补 `TestClient` 用例，登记 S-24。**新增自裁**：S-24（主体种类不符 → 401）、S-25（查不到行 → `TOKEN_REVOKED`，不回 404、不回显令牌）、S-26（缺头由内核出 401，须带 `= None`），见契约 §12 v1.5。**夹具自纠（回报不改）**：conftest 的 `db_conn` 复用 `app/db.py` 的模块级引擎，而 pytest-asyncio 每个用例一个新事件循环——池里留下的 asyncpg 连接属于已关闭的循环，隔一个用例就在 `engine.connect()` 炸 `AttributeError: 'NoneType' object has no attribute 'send'`，表现为「一半通过、一半 skip」，看着像数据库在抖。本轮在本文件内自建 `auth_db`（NullPool，同契约：事务 + 回滚 + 连不上即 skip）先把真库用例跑实，缺陷登记任务板 P-03 请 A 在 conftest 补 `engine.dispose()`；A 修好后 `auth_db` 应删除、全部改回 `db_conn`。**边界复述**：`app/core/auth.py` 只 `SELECT`，不写库、不碰迁移；`app/api/**`、`app/main.py` 未动。设备分支等 P-02 的 `Device` 模型（T4b-2）。 |
| v1.6 | 2026-11-04 | T4b-2（设备会话回查 + 工牌 `elder_id`）完成：`app/core/auth.py` 新增 `_load_device_row`（只读 `device_id / token_hash / status / elder_id`），`resolve_principal` 的 `typ=device` 分支交回 T4a 已定的 `device_identity_from_session`，`_load_staff_token_row` 再加一处 `devices` JOIN 带出绑定长者；**`DeviceLookupNotWiredError`** **连同** **`__all__`** **条目一并删除**（缺口已补齐，不留兼容壳）。新增 10 条用例（假会话设备分派/`locked`/哈希轮换/缺行、工牌未绑设备、ASGI 设备端点、4 条真库），单文件 193 条、全量 200 条、0 skip，ruff check/format、mypy（28 文件）全绿。**夹具转 ORM**：`_db_add_card` 里 `rooms` / `devices` 的裸 SQL 换成 `Room.__table__` / `Device.__table__` 的 Core insert，另加 `_db_add_elder`（`devices.elder_id` 的外键目标只要一个 `name`）与 `_db_patch_device`；零残留用例的清点表加 `elders`（6 张）。**两条口径确认（不新增自裁）**：① `offline` 与 `elder_id IS NULL` 放行——前者是心跳问题（FR-ADM-10），后者是「已配对未绑长者」的中间态，拒了会把正常态变成刷不了卡；② 设备行不存在按 `TOKEN_REVOKED`（S-25 补成四条回查），不回 404、不回显令牌。契约只改 S-22/S-25 的接线事实并追加 §12 v1.6，判定规则一条没动。**踩坑一条**：新写的真库用例先失败——`_db_add_device` 默认 `token_hash=_DB_HASH_PLACEHOLDER`，其前 16 位与签发用的 `DEVICE_TOKEN_HASH[:16]` 不同源，于是「不绑长者也不该拒」的用例被哈希判定拦下。修法不是放宽判定，而是让夹具显式传 `token_hash=DEVICE_TOKEN_HASH`：假会话用 `_device_row()` 助手把列序与哈希同源固定住，真库夹具必须同样同源，否则测的是夹具不是判定。                                                                                                                                                                                                                                                                                    |
| v1.7 | 2026-11-04 | T5（分层测试收口）完成：`tests/test_security.py` 新增 27 条用例（16 条解码侧矛盾载荷参数化 + 10 条兜底与缺口 + 1 条家属缺行参数），单文件 220 条、全量 227 条、0 skip；行覆盖 `security.py` **100%** / `auth.py` 99% / `runtime_context.py` 99%（合计 **99.45%**，T5 前 91.59%），ruff check/format、mypy（28 文件）全绿。**判定规则与契约口径一条没动，本轮不新增自裁。核心发现（这才是 T5 的实质）**：`_validate_payload` 的注释号称「签发前一遍、验签后一遍」，但解码侧那半边此前**一条用例都没有**——把 `decode_token` 里那次调用删掉，旧的 193 条仍然全绿。矛盾载荷只能用**真密钥手工签**（新增 `_signed_payload`），现实中来自旧版本签出的令牌与被改过的白名单，不是理论风险。**三条兜底各有独立用例**：底层库抛意料之外的异常 → 仍 401（不外泄成 500）；异常文案里含密钥 → 就地换成不含信息的异常且不写进日志（AGENTS §4.3）；`_ALLOWED_CLAIMS_BY_TYP` 加了新 `typ` 却没在 `resolve_principal` 加分派分支 → 401 且 `queried == []`（不伪造身份、不查库）。**两处真实缺口关掉**：`_load_guardian_row` 的缺行分支（假会话参数化 + 真库 `test_db_absent_account_row_revokes`，员工/家属各查一次）、`Identity` 缺 `principal_id` 的构造拒绝。**未覆盖 2 行故意不补，理由写进文件不靠口头**（测试文件末尾「T5 交付证据 · 未覆盖行清单」）：`auth.py:88` 的 `AuthError(code=…)` 全仓库无人传入，是死参数，补用例等于把它合法化，交 T6 人审决定删或留；`runtime_context.py:210` 被 `__post_init__` + `frozen/slots` 双重堵死，走到它得先伪造一个绕过类型系统的坏对象。**踩坑（同一坑第 2 次）**：本波新写的「`dev` 大写必须拒」两条用例又用了 `DEVICE_ID.upper()` —— 该常量全是数字，`.upper()` 是空操作，两条断言无声退化成空测试，换成含字母的 `LETTERED_ID` 后才真正变红。v1.2 已在常量处写过这条警告，写新用例时没回看。**教训**：大小写类断言必须先自证 `upper() != 原值` 作为前提；建议在第三次踩坑之前就把这条补进 AGENTS §9。**过程说明**：为证明「删掉判定用例必红」曾临时把解码侧那次调用改成 `pass` 做变异实验，被权限判定当场拦下，随即 `git checkout -- app/core/security.py` 还原（已核对工作区只剩测试文件改动），改用行为证据代替——高风险鉴权文件不留「先破坏再验证」的中间状态。                  |

