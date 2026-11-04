# Task 6 实施计划 · 长者 CRUD 与设备绑定

> 状态：已冻结　｜　版本：v1.1　｜　日期：2026-11-04
> 关联：docs/team_tasks/task-6-长者CRUD与设备绑定.md
> 上游依据：PRD §3.1–3.2、§4.10、AC-18/24（验收）｜技术设计文档 §3.2–3.3、§3.6（实现）｜接口契约 E5–E11、A-01/A-02
> 变更记录见文末

## 1. 目标与边界

本计划对应 **Day 2 任务板的 task-6「长者档案 CRUD + 设备绑定第一层 + 范围过滤」**，不是大纲 Day 6 的三级路由。

本卡只实现冻结契约中的 E5–E11：

| 端点 | 本卡行为 |
| --- | --- |
| `POST /api/elders` | 管理员建档，201；非管理员 403 |
| `GET /api/elders/{elder_id}` | 管理员全量；护工/医生按各自 relation 读负责长者；家属、设备 403 |
| `GET /api/elders` | 管理员全量；护工/医生只列负责范围；无范围为 empty list；家属、设备 403 |
| `PATCH /api/elders/{elder_id}` | 管理员修改允许字段；不提供物理 DELETE；`archived` 本卡只落状态 |
| `POST /api/devices/{device_id}/bind` | 管理员绑定房间与长者，保证 device:elder 1:1 且长者在目标房间床位中 |
| `POST /api/devices/{device_id}/unbind` | 清空 `elder_id`、置 `offline`、轮换 `token_hash`，使旧设备会话 401 |
| `GET /api/devices/{device_id}/elder-candidates` | 管理员或设备本人查本房间 1/2 位候选，返回脱敏姓名 |

**不在本卡范围**：登录接口、JWT 内核、ORM/迁移、router 注册到 `app/main.py`、三条攻击总验收、声纹匹配、主动确认、退房清数据流水线、家属绑定接口、审计表。

### 必须先固定的冲突口径

1. 任务卡写有「家属只读自己范围」，但冻结契约 **A-01** 已明确裁决：**家属读任何长者档案一律 403**。本卡按契约执行，不实现家属档案范围查询。
2. 护工与医生不能只按 `staff_id` 过滤：护工必须匹配 `relation='primary_care'`，医生必须匹配 `relation='doctor'`。
3. 鉴权顺序固定为 **验签 → 判角色 → 判范围 → 判存在**。范围外统一 403，不能通过 404 泄露资源是否存在；管理员查不存在的资源才返回 404。
4. 范围过滤只在 service 层执行。API 层只解析参数、接收当前主体、调用 service、序列化响应，不直接操作 ORM。
5. `audit_logs` 到 Day 9 才建表。本卡按契约 A-02 使用结构化日志留痕，包含 `trace_id`、动作、主体、目标与结果；不得提前建表。
6. `app/main.py` 与 `app/api/__init__.py` 由 task-7 注册 router。Task 6 的测试自行装配临时 FastAPI app，不能为了跑接口测试修改这两个文件。

## 2. 当前工作区与硬前置

截至 2026-11-04，当前分支 `feature-ywd` 位于 `cdbc44b`。所有分支可见提交中均尚无 task-3 / task-4 实现。

现状：

- 已有 Day 1 `0001` 迁移，包含 `rooms / beds / devices / elders / personas`，且 `uq_devices_elder_id` 部分唯一索引已存在。
- 已有 FastAPI 骨架、Settings、结构化日志、`trace_id` 与 `/health`。
- 当前只有 `app/models/base.py`，**Day 1 五表 ORM 类尚不存在**。
- `app/db.py`、`tests/conftest.py`、`app/core/security.py`、`app/core/auth.py`、`app/core/runtime_context.py` 尚未落地。
- task-6 当前在看板中仍为锁定状态。

### 开工门槛（缺一不可）

1. **task-3 已合并**：本地 PostgreSQL、`app/db.py`、`0002`、五张关系/账号表、`tests/conftest.py` 可用，迁移双向验证通过。
2. **Day 1 五表 ORM 已由 A 在 task-3 的 `app/models/**` 写作用域内补齐**。这是当前任务板的所有权缺口：task-6 禁止改 models，没有这些类就无法实施。若 task-3 未包含它们，先让主理人调整 task-3 卡面或补交，不允许 D 越权补。
3. **task-4 已合并**：人员/设备凭证校验、角色依赖、当前主体数据结构、runtime context、领域异常/错误映射可复用。
4. 开工后先阅读 task-3/4 的真实代码，列出实际模型名、DB 依赖名、Principal/Identity 字段、auth dependency、异常类和事务约定；不得凭本计划猜接口名。
5. task-5 不是本卡前置；完整 Day 2 演示与 router 注册由 task-7 收口。

若任一门槛不满足，实现 AI 必须停止并报告阻塞，不得用临时模型、内存仓库、伪造 auth 或平行迁移替代。

## 3. 实施步骤

### Step 0 · 基线与接口核对

- 确认工作树干净，从包含 task-3/4 的集成分支切出 D 的实现分支。
- 跑 `uv run pytest -q` 与 `uv run ruff check app tests`，记录基线。
- 阅读实际 ORM、DB session、auth dependency、错误映射和 conftest。
- 输出一页内的「实际接口映射 + 测试清单」，发现契约冲突就停下，不修改冻结契约。

### Step 1 · 长者 service 垂直切片

文件：`app/services/elder_service.py`

- 管理员：创建、列表、详情、修改。
- 护工/医生：按当前角色强制对应 relation 做列表与详情范围过滤。
- 家属/设备：档案读接口直接拒绝；列表也不返回数据。
- 详情返回 R4 全字段并联表补 `room_no / bed_no`；列表不返回 `birth_date / gender / persona_id`。
- 创建默认值遵循数据库：`status='active'`、`session_seq=0`；允许重名。
- PATCH 仅允许 `name / birth_date / gender / persona_id / status`；忽略或拒绝额外字段，不能客户端传 `session_seq`。
- 改为 `archived` 时只允许改状态；本卡不做删除和后续清理。
- 写操作遵循 task-3 的事务约定；所有越权与写操作按 A-02 留结构化审计日志，不写姓名、生日、token 等敏感值。

### Step 2 · 设备 service 垂直切片

文件：`app/services/device_service.py`

- 绑定前检查设备、目标房间、长者及目标房间内对应床位；长者不在该房间返回 409。
- 更新 `devices.room_id / elder_id`；第二台设备绑同一长者返回 409。
- 依靠现有 `uq_devices_elder_id` 防并发冲突；service 预检查不能替代数据库唯一索引。
- 捕获唯一约束冲突并转换为契约 409，同时恢复 session 可用状态，不能留下 failed transaction。
- 解绑必须同事务完成：`elder_id=NULL`、`status='offline'`、安全随机值经 SHA-256 后轮换 `token_hash`；不明文返回或记录新 secret。
- `bind` 请求中的 `elder_id=null` 必须复用同一解绑逻辑，不能成为绕过 token 轮换的后门。
- 候选集只从 `beds.room_id=devices.room_id AND elder_id IS NOT NULL` 查询，不取 `devices.elder_id` 充当集合，也不返回他房长者。
- 1 人返回 `mode='single'`，2 人返回 `double`；候选字段严格为 `elder_id / bed_no / display_name`，姓名按「姓 + `*` + 末字」脱敏。
- 设备主体只能查询路径中的自身设备；其他设备、护工、医生、家属均 403，管理员可查。

### Step 3 · API 薄层

文件：`app/api/elders.py`、`app/api/devices.py`

- 在两个 API 文件内声明本卡所需 Pydantic v2 请求/响应模型，避免新增越权文件。
- 路径、方法、字段、分页和错误码严格按契约 E5–E11、S-07、S-10、§7。
- `page` 默认 1，`page_size` 默认 20、上限 100；非法值 422。
- 复用 task-4 的当前主体与角色依赖；service 不直接构造 HTTP 响应，API 将领域异常映射为 401/403/404/409/422。
- 所有受保护端点无凭证均 401；角色不足 403；范围外 403；资源不存在按约定顺序处理。
- 响应错误体包含契约要求的 `detail / code / trace_id`，优先复用 task-4 的统一错误机制。

### Step 4 · 测试与回归

文件：`tests/test_elders_api.py`

- 使用 task-3 的真实 PostgreSQL 测试基座，不使用 SQLite 替代 JSON/约束行为。
- 测试内创建临时 FastAPI app，只包含本卡两个 router；不改 `app/main.py`。
- 角色/范围测试可覆盖当前主体依赖以聚焦本卡逻辑；至少一条设备解绑用例走 task-4 的真实设备凭证校验，证明旧 token 返回 401。
- 所有成功与失败的写操作都回查 DB：成功验证字段真实变化，失败验证没有部分写入。
- 测试数据全部虚构，不写真实手机号、身份证或长者信息。

## 4. 最小测试矩阵

| 编号 | 场景 | 核心断言 |
| --- | --- | --- |
| T01 | admin 创建长者 | 201；回查 DB 的姓名/生日/性别/persona；`active`、`session_seq=0` |
| T02 | 护工/医生/家属/设备创建长者 | 均 403；回查 DB 无新增 |
| T03 | admin 详情与列表 | 详情含 R4 + 房间床号；列表无生日/性别/persona；分页总数正确 |
| T04 | 护工/医生读负责长者 | 各自正确 relation 可读；错误 relation 不可读 |
| T05 | 护工读非负责长者 | 403 + 审计字段；范围外存在与不存在均不泄露 404 |
| T06 | 护工/医生列表 | 只含各自负责范围；无分配返回 `items=[]、total=0` |
| T07 | 家属读已绑定/未绑定长者档案 | 两种均 403；列表也 403 |
| T08 | admin PATCH 与非 admin PATCH | admin 回查 DB 字段已变；非 admin 403 且 DB 不变；`archived` 不允许夹带其他字段 |
| T09 | 设备绑定成功 | 200；回查 DB 的 `room_id / elder_id` 与响应一致 |
| T10 | 第二台设备绑同一长者 | 409；回查两台设备，原绑定未被覆盖、新绑定未落库 |
| T11 | 跨房间绑定/设备不存在/非 admin 绑定 | 分别 409/404/403；失败时 DB 不变 |
| T12 | 解绑与旧设备会话重放 | 回查 `elder_id=NULL`、`offline`、`token_hash` 已变化；旧 token 401；新 hash/secret 不进日志 |
| T13 | `bind` 传 `elder_id=null` | 与 unbind 相同地轮换 hash、置 offline；旧 token 401 |
| T14 | 单人间/双人间候选 | 分别 1/2 人；无他房长者；字段裁剪与 `张*芬` 脱敏正确 |
| T15 | 候选接口身份范围 | admin、设备本人 200；设备 B 查设备 A、staff、guardian 均 403；无凭证 401 |

T01–T15 是最小建议分组，可按参数化拆成更多 pytest 用例；任务卡要求的「至少 3 条」只是下限，不应拿 3 条替代上述风险覆盖。

## 5. Task 6 Definition of Done

以下全部满足，才可把 task-6 标为完成：

- [ ] E5–E11 全部实现，且没有新增契约外端点或物理 DELETE。
- [ ] 非管理员 `POST /api/elders` 返回 403。
- [ ] 护工/医生按各自 relation 过滤；护工查非负责长者详情返回 403，家属档案读一律 403。
- [ ] 范围过滤只存在于 service；API 无 ORM 写库和业务规则。
- [ ] 每条写操作测试均有 DB 回查断言；第二台设备绑同一长者返回 409 且 DB 无部分写入。
- [ ] 解绑清空长者、置 offline、轮换 hash，旧设备会话经真实校验返回 401。
- [ ] 单/双人间候选数量、来源、字段和姓名脱敏正确。
- [ ] 结构化审计日志带 `trace_id`，不含 token、secret、手机号、生日等敏感明文。
- [ ] `uv run pytest tests/test_elders_api.py -q` 全绿，且不是 DB 不可用导致的 skip。
- [ ] `uv run pytest -q`、`uv run ruff check app tests`、`uv run ruff format --check app tests` 通过。
- [ ] `git diff --name-only` 只出现本卡允许的五个代码/测试文件；没有 models、alembic、core、main、api/__init__ 改动。
- [ ] 交付报告包含 diff 摘要、T01–T15 断言位置、实际命令输出、人工审查点与未解决项。

### 与「Day 2 当日 DoD」的关系

Task 6 完成只代表当日 DoD 中长者/设备相关条目完成。完整 Day 2 仍需：

- task-3：迁移 upgrade/downgrade 双向可跑；
- task-4/5：错密码、伪造 token、`/sessions/current`、设备会话、账号停用等验收；
- task-7：router 注册、三条攻击用例、Iter 1 演示、全量测试与文档收口；
- task-2：六个提示词模板；
- 全组：至少 5 次小步 commit。

因此实现 AI 不得在 Task 6 报告中宣称「Day 2 全部完成」。

## 6. 可直接交给编码 AI 的提示词

```text
你在康伴（SilverMate）仓库工作，工作目录是 D:/Desktop/kangban。
请只完成 docs/team_tasks/task-6-长者CRUD与设备绑定.md，不要实现大纲 Day 6，也不要顺手完成其他任务卡。

【一、先读与开工门槛】
先阅读：
1. AGENTS.md，尤其 §2、§4、§5、§8、§10；
2. docs/team_tasks/task-6-长者CRUD与设备绑定.md；
3. docs/接口契约_身份与权限.md 的 §2–§4、§6–§10，重点是 E5–E11、A-01、A-02；
4. docs/康伴3D数字人_PRD.md 的 §3.1–3.2、FR-ADM-01/03/04、AC-18/24；
5. docs/康伴3D数字人_技术设计文档.md 的 §3.2–3.3、§3.6；
6. docs/plans/task-06-长者CRUD与设备绑定.md；
7. task-3/4 合并后的真实代码：app/db.py、app/models/**、tests/conftest.py、app/core/security.py、app/core/auth.py、app/core/runtime_context.py 及实际错误映射。

开工前检查：
- task-3、task-4 已合并且基线测试可跑；
- rooms、beds、devices、elders、personas 以及 Day 2 关系表都有可用 ORM；
- 当前主体、角色依赖、设备凭证校验、DB session 和测试事务基座都已存在。
如果缺任何一项，立即停止，不修改文件，报告「缺什么、应由哪张任务卡提供、为什么不能由本卡代做」。不要创建临时 ORM、内存 DB、假 auth 或新迁移来绕过。

先用简短列表反馈你读到的实际模型名、session 依赖、Principal/Identity 字段、auth dependency、异常映射和事务约定，然后按这些真实接口实施；不要猜名字。

【二、允许修改的文件，只有五个】
- app/services/elder_service.py
- app/services/device_service.py
- app/api/elders.py
- app/api/devices.py
- tests/test_elders_api.py

禁止修改：app/models/**、alembic/**、app/core/**、app/main.py、app/api/__init__.py、docs/**、pyproject.toml、uv.lock，以及其他任务卡文件。
不要新增依赖，不要注册主应用 router，不要实现登录接口，不要改 README 或任务状态。

【三、架构约束】
- FastAPI + Pydantic v2 + SQLAlchemy 2.x async + 本地 PostgreSQL。
- services 是唯一写库层；API 只做 Pydantic 校验、接收当前主体、调用 service、异常映射与序列化。
- 范围过滤只能在 service。不要靠前端隐藏按钮，不要在 API 直接拼查询。
- I/O 全 async，全量 type hint，沿用现有事务与日志约定。
- service 不直接返回 HTTP 响应；优先复用 task-4 的领域异常和错误体机制。
- 审计本卡只写结构化日志，带 trace_id、audit_action、actor、target、result；audit_logs 表 Day 9 才建。
- 日志和响应不得泄露 password_hash、token_hash、device_secret、JWT、手机号、完整生日等敏感值。

【四、端点与业务口径】
只实现契约 E5–E11：
1. POST /api/elders：admin 201；其他主体 403；允许重名；DB 默认 active、session_seq=0。
2. GET /api/elders/{elder_id}：admin 全量；care_worker 只能读 relation=primary_care；doctor 只能读 relation=doctor；guardian 和 device 一律 403。详情按契约带 R4 全字段及 room_no、bed_no。
3. GET /api/elders：admin 全量；staff 只返回各自负责范围，无范围返回空列表；guardian/device 403。page 默认 1，page_size 默认 20、最大 100；列表不返 birth_date、gender、persona_id。
4. PATCH /api/elders/{elder_id}：仅 admin；只允许 name、birth_date、gender、persona_id、status；客户端不得修改 session_seq；archived 只允许改状态，不做物理删除和清理流水线。
5. POST /api/devices/{device_id}/bind：仅 admin；更新房间与长者；长者必须在目标房间床位中，否则 409；第二台设备绑同一长者 409；设备不存在 404。
6. POST /api/devices/{device_id}/unbind：仅 admin；elder_id=NULL、status=offline，并轮换 token_hash，使旧设备会话 401。bind 传 elder_id=null 时必须走同一解绑逻辑，不能绕过 hash 轮换。
7. GET /api/devices/{device_id}/elder-candidates：admin 或设备本人 200；设备 B 查设备 A、staff、guardian 均 403。候选只来自设备房间的 beds 中 elder_id 非空记录，不能用 devices.elder_id 代替集合，不能混入他房长者。1 人 single、2 人 double；字段仅 elder_id、bed_no、display_name；display_name 按「姓+*+末字」脱敏，例如张桂芬→张*芬。

权限与错误顺序必须遵守：验签→角色→范围→存在。
- 无凭证/凭证失效 401；角色不足 403；范围外 403；admin 查不存在资源 404。
- 范围外用户不能通过 404 判断资源是否存在。
- 护工和医生必须按各自 relation 过滤，不能只查 staff_id。
- 冻结契约 A-01 优先于任务卡概括文字：家属即使已绑定，也禁止读长者档案。

设备冲突要同时依靠 service 校验与现有 uq_devices_elder_id 部分唯一索引。捕获 IntegrityError 后映射 409，并正确 rollback/恢复事务，不能留下部分写入。token_hash 轮换使用安全随机值再做 SHA-256，不明文返回或记录新 secret。

【五、测试要求】
所有测试放 tests/test_elders_api.py，使用 task-3 的真实 PostgreSQL conftest；DB 不可用必须明确 skip 并打印原因，完成验收时不能把 skip 当通过。
测试内自行创建临时 FastAPI app 并 include 本卡 routers，不修改 app/main.py。

至少覆盖计划中的 T01–T15：
- admin 创建与 PATCH 成功并回查 DB；
- 各非 admin 创建/修改均 403 且 DB 不变；
- admin 详情/列表字段、分页和隐私字段裁剪；
- 护工/医生正确 relation 可读、错误 relation/非负责 403、列表范围正确、空范围为空；
- 家属已绑定和未绑定读档案均 403；
- 范围外 403 不泄露资源存在性；
- 绑定成功回查 DB；跨房 409；第二台设备绑同人 409 且 DB 不变；设备不存在 404；非 admin 403；
- unbind 后回查 elder_id NULL、offline、token_hash 已变，并用 task-4 的真实设备凭证校验证明旧 token 401；
- bind 的 elder_id=null 与 unbind 同样使旧 token 401；
- 单人间 1 人、双人间 2 人、无他房长者、脱敏及字段裁剪；
- 候选接口 admin/设备本人可用，其他设备和其他角色 403，无凭证 401。

每条成功或失败的写操作用例都必须回查 DB，不能只断言 HTTP 状态码。测试要断言真实业务结果，禁止无断言、只打印、 mocked 到失去意义的幻觉测试。

【六、执行顺序】
1. 核对前置和真实接口，记录基线测试。
2. 实现 elder_service 及对应 API，先跑创建、读取、范围、PATCH 测试。
3. 实现 device_service 及对应 API，再跑绑定、解绑、候选、旧 token 测试。
4. 自审 diff：重点检查家属 403、relation 过滤、范围先于存在、唯一冲突事务恢复、null bind 不绕过 token 轮换、敏感日志。
5. 运行：
   uv run pytest tests/test_elders_api.py -q
   uv run pytest -q
   uv run ruff check app tests
   uv run ruff format --check app tests
   git diff --name-only
6. 若失败，只修本卡五个文件内的问题；若根因在 task-3/4，停止并报告给对应主责，不要替他们改。

【七、停止条件与报告】
上述测试和静态检查全绿、diff 只含五个允许文件后停止，不扩展功能。
最终报告必须包含：
- diff 摘要；
- 实际接口映射；
- T01–T15 各自测试函数名和关键断言位置；
- 四条验证命令的真实结果，明确 passed 数量及是否有 skip；
- 人工审查点（范围过滤、设备解绑、唯一冲突、敏感日志）；
- 未解决项或文档冲突。
只能声明 Task 6 完成，不能宣称 Day 2 全部 DoD 完成；router 注册与当日总验收归 task-7。
```

## 7. 变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1.0 | 2026-11-04 | 初版：核对 Task 6 工作区差距、冻结契约冲突、实施步骤、T01–T15 测试矩阵、Task 6 DoD 与编码 AI 提示词 |
| v1.1 | 2026-11-04 | 状态改为已冻结：E5–E11 与 T01–T15 已交付；补充 §7 的 400/422 分界、未绑定设备候选、唯一索引 rollback 兜底与审计脱敏断言；router 注册与当日总验收仍归 task-7 |
