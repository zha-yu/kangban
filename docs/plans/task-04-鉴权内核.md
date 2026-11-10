# task-04 · 鉴权内核实施计划

| 项      | 内容                                                                                               |
| ------ | ------------------------------------------------------------------------------------------------ |
| 状态     | 进行中（T1、T2、T3、T4a 已完成；T4b 等 `task-3` 建表后接线）                                                             |
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

| 编号 | 子任务          | 主要产出                                      | 依赖                           |
| -- | ------------ | ----------------------------------------- | ---------------------------- |
| T1 | 密码安全原语       | `hash_password`、`verify_password`，统一预哈希策略 | 无数据库                         |
| T2 | JWT 安全原语     | 四类令牌签发/解码、Settings 校验、载荷校验                | 无数据库                         |
| T3 | 运行时身份上下文     | 不可变 `Identity`、contextvar 生命周期管理          | 无数据库                         |
| T4a | 鉴权纯判定层 | Bearer 解析、三类主体解码、库里事实比对、身份构造、角色判定（可测、不碰库） | 无数据库 |
| T4b | FastAPI 接线 | `get_session` 回查三张表、主体依赖、`WWW-Authenticate` 响应头 | `task-3` ORM 与 `get_session` |
| T5 | 分层测试         | 纯函数测试先行，数据库与依赖测试后接线                       | T1–T4                        |
| T6 | 审查与交付报告      | diff 摘要、五类失败断言点、人审记录                      | 全部实现完成                       |

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
   |  |  |  
   | --- | --- |
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
> 实现并测试；第 3 条里「查询哪张表、怎么拿 session」属 T4b，等 `task-3` 的 `app/db.py`
> 与 ORM 就绪后接线，**判定不许在依赖层重写一遍**。本轮新决策登记为契约 **S-19 ~ S-23**：
> S-19 分派不读未验签 `typ`、S-20 角色取库、S-21 设备只 `locked` 拒、S-22 工牌归属/状态/到期
> 四条 + **故意不比对 `staff_tokens.scope`**、S-23 Bearer 头语法。
> 其中 S-22 的未做项是显式留白：R9 的 `scope` 是单个 text，载荷是列表，切分口径在 PRD/TD
> 均无出处，等 Day 9 的 E14 定完口径再补比对。
> 第 1 条的 `WWW-Authenticate: Bearer` 是**响应头**，不在纯判定层产生，由 T4b 的依赖/错误映射落。

### 5.5 T5：测试实施顺序

1. **无数据库阶段**：先完成密码、JWT、runtime context 测试，保证可与 `task-3` 并行。
2. **数据库接线阶段**：复用 `task-3` 的 PostgreSQL 事务夹具构造员工、家属、设备和工牌记录，验证真实回查与即时失效。
3. 无数据库环境只能按 `conftest` 明确 skip 并输出原因；正式合并验收必须在有库环境执行，不能把 skip 计作通过。
4. 依赖测试可在 `test_security.py` 内定义最小 FastAPI 测试路由，不修改 `app/api/**` 或 `app/main.py`。
5. 过期用例通过显式构造过去时间的 `exp`，不新增时间冻结依赖。

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

- [ ] 密码哈希/校验支持超过 72 字节及 Unicode 密码，错误密码不能通过。
- [ ] Access、Refresh、设备、工牌令牌均可按契约签发和解码，TTL 来自 Settings 或工牌显式参数。
- [ ] 空 `jwt_secret` 在签发和校验两侧均明确失败。
- [ ] 五类必测失败全部有独立用例和有效断言：伪造签名、过期、`typ` 混用、`tv` 失配、空 secret 拒签，结果为 401 或明确异常。
- [ ] 人员账号 `disabled`、`token_version+1` 后旧 Access/Refresh 均不能通过相应校验。
- [ ] 设备哈希轮换/解绑后旧设备会话 401；工牌记录不存在或过期 401。
- [ ] 人员 token 不能调用设备依赖，Refresh 不能充当 Access，设备 token 不能通过员工角色检查。
- [ ] 有效员工但角色不足时 `require_roles` 返回 403；主体种类不符返回 401。
- [ ] `Identity` 仅在校验成功后进入 context，请求结束及异常后均恢复为空；无任何客户端/模型 `elder_id` 注入入口。
- [ ] 日志和测试失败输出不包含原始密码、完整 token 或 `jwt_secret`。

### 7.2 工程验收

```bash
uv run pytest tests/test_security.py -q
uv run pytest -q
uv run ruff check app tests
```

- [ ] 上述命令在有 PostgreSQL 测试库的环境通过；数据库用例无静默 skip。
- [ ] 仅修改本任务允许的四个实现/测试文件，不新增依赖，不触碰其他任务写作用域。
- [ ] `security.py`、`auth.py` 已人工逐行审查 diff。
- [ ] 交付报告包含 diff 摘要，以及五类失败用例各自的测试名和断言点。

## 8. 交付与后续衔接

1. 向 `task-5` 说明密码、四类 JWT、Refresh 解码和身份依赖的公开函数及异常语义。
2. 向 `task-6` 说明 `current_staff` / `current_guardian` / `current_device` 和 `require_roles` 的使用方式；范围过滤不在本内核内实现。
3. 向 `task-7` 提供伪造签名、过期、类型混用、版本失配和设备解绑用例位置，供攻击测试与全链路验收复用。
4. 若实现中发现契约未覆盖的新决策，先登记并请求裁决，不在代码中静默引入新的载荷名、有效期或权限语义。

---

## 9. 变更记录

| 版本   | 日期         | 变更                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---- | ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| v1.0 | 2026-11-04 | 首版计划：T1–T6 拆解、实现步骤、风险与验收标准                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| v1.1 | 2026-11-04 | T1 完成：`security.py` 密码哈希/校验 + `tests/test_security.py`；bcrypt cost 与 NFKC 预哈希登记为契约 S-11/S-12                                                                                                                                                                                                                                                                                                                                                                                                                         |
| v1.2 | 2026-11-04 | T2 完成：四类令牌签发 + 纯解码（不含回查库）落地，88 条单测、全量 95 条、ruff check/format、mypy 全绿。<br />**事实纠正**：原计划与代码注释假设「pyjwt 的 `leeway` 不作用于 `iat`」，pyjwt 2.15.1 实测**推翻**——`leeway` 对 `iat` 有效，但它是全局旋钮、会连带放宽 `exp`。实现做法不变（关 `verify_iat` + 自判 ≤60s），理由换成实测成立的那条，注释与用例已同步（`test_pyjwt_leeway_is_the_wrong_knob_for_iat_skew`）。<br />**测试自纠**：`STAFF_ID`/`GUARDIAN_ID`/`DEVICE_ID` 三个常量全是数字，`.upper()` 是空操作，导致两条「大写 UUID 规范化」用例静默退化为空测试；改用一个含字母的 `LETTERED_ID` 并在用例内断言 `upper() != 原值`，防止再次无声失效。<br />新增契约自裁 S-13/S-14/S-15/S-16（见契约 §12 v1.2） |
| v1.3 | 2026-11-04 | T3 完成：`app/core/runtime_context.py` 落地 `Identity`（frozen + slots，`__post_init__` 强制字段矩阵）与 contextvar 载体（`set/get/require/reset/identity_scope/as_mapping`），新增 25 条用例，单文件 113 条、全量 120 条、ruff check/format、mypy（`security.py` + `runtime_context.py`）全绿。<br />**复用而非重造**：把 `security.py` 的 `_canonical_uuid` / `_STAFF_ROLES` / `_normalize_scope` 提为公开 `canonical_uuid` / `STAFF_ROLES` / `normalize_scope`，身份构造直接调用——令牌与身份必须同一套判定。<br />**实现自查**：`identity_scope` 初版漏了 `@contextmanager`，返回裸 generator，被 T3 用例当场抓到（`TypeError: 'generator' object does not support the context manager protocol`）——「异常必回滚」这条不写用例就根本测不出来。<br />**登记**：新增契约自裁 S-17（身份字段矩阵，`staff`/`guardian` 不容纳 `elder_id`）、S-18（注入只收 `Identity` 实例、无身份抛 `IdentityNotSetError`、写入必须成对回滚）。<br />**回报但不擅改**：`uv run mypy app/core` 在 Day 1 的 `app/core/context.py:44`、`app/core/trace.py:45` 各报一条 `union-attr`（`bool(value) and` 之后 mypy 不收窄），不在 task-4 写作用域内，等主理人决定是否顺手修。 |
| v1.4 | 2026-11-04 | T4 拆成 T4a（纯判定层，本轮完成）/ T4b（`get_session` 接线，等 task-3）。T4a 落地 `app/core/auth.py`：`bearer_token_from_header`、`decode_principal_token`、`decode_refresh_token`、三类 `*_identity_from_*` 构造函数、`device_hash_prefix_matches`、`staff_card_lookup_key`、`check_roles`，异常系 `AuthError` 五种子类各带契约 §7 的 `code`；新增约 40 条用例，单文件 145 条、全量 152 条、ruff check/format、mypy（三个 auth 文件）全绿。<br />**判定与接线分离**：`auth.py` 只吃「从库里读出的事实」（`db_role` / `db_status` / `db_token_hash` / `db_expires_at` 等关键字参数），SQL 与 session 留在 T4b，避免同一条规则在依赖层重写第二份。<br />**两处口径纠正**：`decode_principal_token` 的三类尝试循环原先注释写「先判 typ 再验签」，实为逐类完整验签、过期在 typ 核对之前命中——注释按实现改正；员工角色越界的用例前提写错（漂移判定先命中，永远走不到构造），改成显式断言「库里脏角色进不了身份」。<br />**lint 自纠**：4 个契约 §7 响应码常量里 3 个被 ruff `S105` 误判成硬编码口令，加 `# noqa: S105` 并在上一行写清「是响应码字面量，不是密钥」，不放宽全局规则。<br />**登记**：新增契约自裁 S-19 ~ S-23（见契约 §12 v1.4），其中 S-22 明确记录工牌本轮**不比对** `staff_tokens.scope` 及原因。 |
