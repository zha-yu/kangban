# Day 2 任务板 · 身份与权限地基

> **事实口径**：`docs/康伴3D数字人_PRD.md`、`docs/康伴3D数字人_技术设计文档.md`。
> **排期口径**：`docs/康伴3D数字人_大纲.md` Day 2。
> 本目录是任务板的人读副本：状态与认领以这里为准，改卡面需全组同意。

## 一、看板（2026-11-04 建板，7 张卡）

| id | 内容 | 主责 | 状态 | 前置 | 写作用域 |
| --- | --- | --- | --- | --- | --- |
| `task-1` | Step0 身份与权限契约冻结 | A–E 全员 | ✅ 已完成（`707bdf6`） | — | `docs/接口契约_身份与权限.md` |
| `task-2` | 提示词模板库 v1（6 个模板） | E | ⬜ 待认领 | — | `docs/prompts/` |
| `task-3` | Step1 库与模型基座（5 表 + `0002` 迁移 + conftest） | A | ⬜ 可认领 | `task-1` ✅ | `docker-compose.yml`、`app/db.py`、`app/models/`、`alembic/versions/`、`tests/conftest.py` |
| `task-4` | Step2 鉴权内核（security / 依赖注入 / runtime_context） | B | ⬜ 可认领 | `task-1` ✅ | `app/core/security.py`、`app/core/auth.py`、`app/core/runtime_context.py`、`tests/test_security.py` |
| `task-5` | Step3 登录与凭证接口 | C | 🔒 锁定 | `task-3`、`task-4` | `app/services/auth_service.py`、`app/api/auth.py`、`tests/test_auth_api.py` |
| `task-6` | Step4 长者 CRUD + 设备绑定 + 范围过滤 | D | 🔒 锁定 | `task-3`、`task-4` | `app/services/{elder,device}_service.py`、`app/api/{elders,devices}.py`、`tests/test_elders_api.py` |
| `task-7` | Step5 验收收口（攻击用例 + router 注册 + DoD） | E | 🔒 锁定 | `task-5`、`task-6` | `tests/test_auth_attacks.py`、`app/main.py`、`app/api/__init__.py`、`README.md` |

图例：⬜ 待认领 · 🚧 进行中 · ✅ 已完成 · 🔒 锁定（前置未完成） · ⛔ 阻塞

## 二、解锁节奏

```
task-1 ✅ ─┬─► task-3   ◀ 现在可认领（A）
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
- [ ] 非管理员 `POST /elders` → 403；护工查非负责长者 → 403（AC-18）
- [ ] 写操作测试都有「回查 DB」断言；≥3 条 elders 用例
- [ ] 3 条攻击用例入库；`pytest` 全绿；`ruff check app tests` 干净
- [ ] `git log` ≥5 次小步 commit；`docs/prompts/` 有 6 个模板
