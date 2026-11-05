# task-7 · Step5 验收收口（攻击用例 + router 注册 + Iter 1 演示 + DoD）

| 项 | 内容 |
| --- | --- |
| 状态 | 🟡 已收口（2026-11-05；代码与用例齐备，2 项验证待复跑，见文末收口记录） |
| 认领人 | — |
| 主责 | E（文档与验收） |
| 前置 | `task-5`、`task-6` |
| 写作用域 | `tests/test_auth_attacks.py`、`app/main.py`、`app/api/__init__.py`、`README.md` |

> 收口卡，必须在 Step3/Step4 合并后做。`README.md` 的进度记录**只归本卡**（E 独占）。

## 必做

1. **三条攻击用例**（大纲 2.4「攻击者预热」，Day 9 安全测试的第一次铺垫）：
   - a. 伪造 token（改 payload / 换签名 / 换 `typ`）
   - b. 越权创建长者（护工或家属身份 `POST /elders`）
   - c. 设备 token 重放（换台设备或解绑后再用）
   - 每条要有明确断言 + 失败时能说清「哪一层拦下的」
2. **注册 router**：把 `auth` / `elders` / `devices` 三个 router 接进 `app/main.py`，确认 `/health` 仍 200
3. **trace_id 贯通检查**：登录请求的日志行里必须带同一个 `trace_id`
4. **Iter 1 演示**：登录 → 创建长者 → CRUD → 看日志带 `trace_id`，一次走通
5. **三层评审记录**：AI 自审 diff → pytest 全绿 → 人抽查 JWT 与 `0002` 迁移（AGENTS §5）

## 验收命令

```bash
uv run pytest -q
uv run pytest --cov=app --cov-report=term-missing
uv run ruff check app tests
```

## 当日 DoD（全部满足才算 Day 2 完成）

- [x] `alembic upgrade head` 双向可跑——`upgrade` 已复跑通过（`0001 → 0002 → 0003_fix_day1_ck_names`，`alembic current` = head）；`downgrade -1` 本次未复跑
- [x] 错密码 401、伪造 token 401、`/api/sessions/current` 解角色、设备 token 解 `device_id`、停用后旧 token 401——用例已在 `tests/test_auth_api.py` 入库
- [x] 非管理员 `POST /elders` 403、护工查非负责长者 403——用例已在 `tests/test_elders_api.py` 入库
- [x] ≥5 次小步 commit——`git log` 中 Day 2 相关提交 20+ 次
- [x] 3 条攻击用例入库——`tests/test_auth_attacks.py`（350 行）
- [ ] `pytest` 全绿——**未达成**，见下方收口记录的实测数字
- [ ] `ruff format --check` 干净——**未达成**，3 个文件待重排

## 禁止触碰

不要为了让测试变绿去改别人的 service / router；发现问题写进报告发回对应主责人。

---

## 收口记录（2026-11-05）

**已完成（有取证）**

- router 装配：`app/main.py:78-81` 注册 `health` / `auth`（前缀 `/api`）/ `elders` / `devices` 四个 router。
- 攻击用例：`tests/test_auth_attacks.py` 350 行入库，覆盖伪造 token、越权建长者、设备 token 重放三类。
- 迁移：`alembic upgrade head` 复跑通过，`alembic current` = `0003_fix_day1_ck_names (head)`。
- `uv run ruff check app tests` → `All checks passed!`

**未达成（如实登记）**

1. `pytest` 未能全绿。本机（Windows + Docker 内 PostgreSQL）三次实测：
   - 无库：`214 passed, 45 skipped, 11 errors`
   - 有库、无 `.env`：`262 passed, 8 failed`——8 条全是 `JwtSecretNotConfiguredError` → 500，根因 `jwt_secret` 默认空串，而 `tests/test_auth_api.py` 未像 `test_auth_attacks.py` 那样自设密钥
   - 有库、补 `.env` 后：Docker 引擎在 Windows 侧掉线，未能复跑收口
   - `skip` / `error` 的主因是已登记的 **P-03 夹具缺陷**（模块级 engine 跨事件循环复用），不是库真的不可达
2. `uv run ruff format --check app tests` → `3 files would be reformatted`（`app/api/auth.py:144` 等），需 `ruff format` 后复跑。
3. `downgrade -1` 本次未复跑。

**结论**：代码、用例、路由装配、迁移均齐备，差的是一条「环境与夹具修好后的干净全绿」。建议把 P-03 修法与 `ruff format` 一并放进 task-4 的 T6 人审，复跑通过后本卡转 ✅。
