# task-7 · Step5 验收收口（攻击用例 + router 注册 + Iter 1 演示 + DoD）

| 项 | 内容 |
| --- | --- |
| 状态 | 🔒 锁定（待 `task-5`、`task-6`） |
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

- [ ] `alembic upgrade head` / `downgrade -1` 双向可跑
- [ ] 错密码 401、伪造 token 401、`/auth/me` 解角色、设备 token 解 `device_id`、停用后旧 token 401
- [ ] 非管理员 `POST /elders` 403、护工查非负责长者 403
- [ ] ≥5 次小步 commit
- [ ] 3 条攻击用例入库

## 禁止触碰

不要为了让测试变绿去改别人的 service / router；发现问题写进报告发回对应主责人。
