# Day 2 任务板 · 身份与权限地基

> **事实口径**：`docs/康伴3D数字人_PRD.md`、`docs/康伴3D数字人_技术设计文档.md`。
> **排期口径**：`docs/康伴3D数字人_大纲.md` Day 2。
> 本目录是任务板的人读副本：状态与认领以这里为准，改卡面需全组同意。

## 一、看板（2026-11-04 建板，7 张卡）

| id | 内容 | 主责 | 状态 | 前置 | 写作用域 |
| --- | --- | --- | --- | --- | --- |
| `task-1` | Step0 身份与权限契约冻结 | A–E 全员 | ✅ 已完成（`707bdf6`） | — | `docs/接口契约_身份与权限.md` |
| `task-2` | 提示词模板库 v1（6 个模板） | E | ⬜ 待认领 | — | `docs/prompts/` |
| `task-3` | Step1 库与模型基座（5 表 + `0002` 迁移 + conftest） | A | ✅ 已完成 | `task-1` ✅ | `docker-compose.yml`、`app/db.py`、`app/models/`、`alembic/versions/`、`tests/conftest.py` |
| `task-4` | Step2 鉴权内核（security / 依赖注入 / runtime_context） | B | 🚧 进行中（判定层 `16513da` + 人员/工牌/设备接线全部落地，剩 T5 测试收口与 T6 人审） | `task-1` ✅ | `app/core/security.py`、`app/core/auth.py`、`app/core/runtime_context.py`、`tests/test_security.py` |
| `task-5` | Step3 登录与凭证接口 | C | 🔒 锁定 | `task-3`、`task-4` | `app/services/auth_service.py`、`app/api/auth.py`、`tests/test_auth_api.py` |
| `task-6` | Step4 长者 CRUD + 设备绑定 + 范围过滤 | D | ✅ 已完成（2026-11-04） | `task-3`、`task-4` | `app/services/{elder,device}_service.py`、`app/api/{elders,devices}.py`、`tests/test_elders_api.py` |
| `task-7` | Step5 验收收口（攻击用例 + router 注册 + DoD） | E | 🔒 锁定 | `task-5`、`task-6` | `tests/test_auth_attacks.py`、`app/main.py`、`app/api/__init__.py`、`README.md` |

图例：⬜ 待认领 · 🚧 进行中 · ✅ 已完成 · 🔒 锁定（前置未完成） · ⛔ 阻塞

> 交付说明（task-3，2026-11-04）：5 表 ORM + `0002` 迁移 + conftest + 最小版 compose 已落地，
> 另加 `0003` 迁移修正 `0001` 的 4 个 CHECK 双前缀命名（0001 本体未动）；验证全绿
> （ruff / mypy / pytest / alembic 升降级闭环），详见 `docs/plans/task-03-库与模型基座.md`。
> conftest 实际为主库事务回滚隔离（卡面「silvermate_test」未采用，决策在计划文档）。

> 交接说明（task-4，2026-11-04，B；T4b-2 接线后更新）：`app/core/auth.py` 已可用于路由层——
> `Depends(get_current_principal)` / `current_staff` / `current_guardian` / `current_device` /
> `require_roles("admin")`，异常系 `AuthError` 五类各带契约 §7 的 `code`。
> **`task-5` / `task-6` 请注意三件事**：① 401/403 的**响应体与 `WWW-Authenticate` 头是 `task-5` 的异常处理器**
> 的活（本卡只保证异常类型与码对，见契约 S-26）；② **设备主体已可用**——`typ=device` 回查
> `devices`，`Identity.elder_id` 即该设备绑定的长者（未绑定为 `None`），`current_device` 已在真实
> ASGI 链路上测通，`task-6` 不必再自己查这张表拿身份；工牌主体的 `elder_id` 同源（JOIN `devices`）。
> ③ 工牌**仍未比对** `staff_tokens.scope` 与载荷 `scope`（契约 S-22）：Day 9 的 E14 之前必须先定
> `scope` 的存储口径，否则「刷卡能干什么」没有可判定的依据。
> 另：本卡在 `tests/test_security.py` 内临时自建 `auth_db` 夹具绕开 P-03，A 修 conftest 后会删掉。

> 交付说明（task-6，2026-11-04，D）：E5–E11 全部实现，角色/范围判定与写库只在
> `app/services/{elder,device}_service.py`；解绑与 `bind(elder_id=null)` 共用同一逻辑并轮换
> `token_hash`，唯一索引冲突 rollback 后映射 409。`tests/test_elders_api.py` 26 条用例、
> 全量 `226 passed`，`ruff check` / `format --check` 全绿；router 注册与当日总验收仍归 `task-7`。

## 二、解锁节奏

```
task-1 ✅ ─┬─► task-3 ✅ 已完成（A）
           └─► task-4   ◀ 现在可认领（B）

task-3 ─┐
task-4 ─┴─► task-5 ─┐
        └─► task-6 ─┴─► task-7

task-2 全程独立，谁手快谁先做（模板库，不依赖任何卡）
```

## 三、认领与协作规则

1. **认领**：把自己的名字写进对应卡的「认领人」栏，状态改 🚧 进行中；完成后改 ✅ 并写一行交付说明。
2. **写作用域**：只动自己卡面列出的文件。`app/main.py`、`app/api/__init__.py`、`app/models/__init__.py` 各自独占，其他人一律不改。
3. **高风险文件**（AGENTS §5：`app/core/security.py`、`app/core/auth*.py`、`app/api/auth*.py`、`app/models/**`、`alembic/versions/**`）必须人工逐行读 diff 才能合并。
4. **冲突处理**：卡面写作用域重叠时，先改卡再改代码，不许两人同时动同一个文件。
5. **禁止**为了让自己的测试变绿去改别人的 service / router。

## 四、四项待裁决 → 已裁决（2026-11-04，见契约 §8 / §9）

| # | 问题 | 结论 | 契约位置 |
| --- | --- | --- | --- |
| **D1** | 登录用什么标识？ | `phone` 登录，NOT NULL + 唯一索引（唯一索引随 `0002` 建，不动 `0001`） | S-02 |
| **D2** | 设备首次怎么拿到凭证？ | 两层：长期 secret 只存 `sha256` 进 `devices.token_hash`；平板换 24h 会话凭证，载荷带 `tv`=哈希前 16 位，每次回查，解绑轮换即全线失效 | S-03、§5.3 |
| **D3** | 双人间候选返回哪些字段？ | `{elder_id, bed_no, display_name}`，`display_name` 脱敏为「姓 + `*` + 末字」 | S-06、E11 |
| **D4** | 工牌 token 做到哪一步？ | 只建 `staff_tokens` 表 + 定载荷，接口留 Day 9 | A-04 |

> 四项已冻结，`task-3` 不必再等；若实现中发现与契约冲突，以契约为准改代码，改契约要走裁决记录。

## 五、当日 Definition of Done

- [ ] `alembic upgrade head` / `downgrade -1` 双向可跑
- [ ] 错密码 401、伪造 token 401、`/auth/me` 解出角色、设备 token 解出 `device_id`
- [ ] 账号停用后旧 token 401（AC-24）
- [x] 非管理员 `POST /elders` → 403；护工查非负责长者 → 403（AC-18）
- [x] 写操作测试都有「回查 DB」断言；≥3 条 elders 用例
- [ ] 3 条攻击用例入库；`pytest` 全绿；`ruff check app tests` 干净
- [ ] `git log` ≥5 次小步 commit；`docs/prompts/` 有 6 个模板

## 六、执行中发现的偏差（只登记，不裁决）

> 本节由发现者登记事实，**不改 §四 的裁决结论、不改别人的卡**。处理人与时机由主理人指派。

| # | 发现日期 | 现象（读到的事实） | 与哪条已冻结口径冲突 | 影响谁 | 建议处理人 |
| --- | --- | --- | --- | --- | --- |
| **P-01** | 2026-11-04 | `alembic/versions/0002_day2_identity.py` 第 42 行与第 57 行：`staff_accounts.phone` 与 `guardian_accounts.phone` 均为 `nullable=True`，且 `0002` 里没有 phone 的唯一索引（该迁移只建了 `ix_elder_guardians_guardian_id_status`） | §四 **D1** / 契约 **S-02**：「`phone` 登录，NOT NULL + 唯一索引（唯一索引随 `0002` 建，不动 `0001`）」 | `task-5`（C）：E1 按手机号查人会查出多行，`status` 与 `token_version` 不知道该用哪一行；越权与即时失效判定随之失真 | A（补 `0004` 迁移：`SET NOT NULL` + `CREATE UNIQUE INDEX`），或走裁决把 S-02 翻案并同步 §四 |
| **P-02** | 2026-11-04 | `app/models/` 目前只有 `base / guardian / relation / staff` 四个模块，导出 5 个 ORM 类；**`devices` 表在库里存在（列与 CHECK 齐）但没有 ORM 类** | 无冲突，属卡面前置缺口（`task-4` 的 T4b 需要读 `devices.token_hash` / `status` / `elder_id`） | `task-4`（B）设备会话回查；`task-6`（D）设备绑定接口同样要读这张表 | ✅ **已闭环**：A 交付 `Device` ORM（`app/models/organization.py`，`f2fb1a0`），B 的 T4b-2 已接 `_load_device_row` 并删掉 `DeviceLookupNotWiredError`（见 `docs/plans/task-04-鉴权内核.md` §9 v1.6）。`task-6` 可直接复用 `current_device`，身份里的 `elder_id` 就是设备绑定长者 |
| **P-03** | 2026-11-04 | `tests/conftest.py` 的 `db_conn` 复用 `app/db.py` 的**模块级引擎单例**，而 `pyproject.toml` 钉 `asyncio_default_fixture_loop_scope = "function"`（每用例一个新事件循环）。池留下的 asyncpg 连接绑在**已关闭的上一个循环**上，隔一个用例就在 `engine.connect()` 抛 `AttributeError: 'NoneType' object has no attribute 'send'` → conftest 判成「PostgreSQL 不可达」而 `skip`。实测：`-k test_db` 9 条里稳定 5 通过 / 4 skip，交替出现，看着像库在抖 | 无口径冲突，属夹具缺陷；但直接顶撞 `task-4` 计划 §5.5 第 3 条「不能把 skip 计作通过」与 DoD「数据库用例无静默 skip」 | 所有用 `db_conn` 的卡：`task-4`（B，本轮已在本文件内用 NullPool 自建 `auth_db` 绕过，并在 P-03 关闭后删掉改回）、`task-5`（C）、`task-6`（D）、`task-7`（E 的攻击用例） | A（二选一：`db_conn` teardown 里 `await engine.dispose()`，或测试期用 `NullPool`）。另建议把 conftest 的 skip 原因按「连不上」与「连接被跨循环复用」分两类——现在两类都写成「不可达」，会把池缺陷读成环境问题 |

> P-01 ~ P-03 都不在登记者的写作用域内，登记者不会代改。
