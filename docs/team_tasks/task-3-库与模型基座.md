# task-3 · Step1 库与模型基座（5 张表 + `0002` 迁移 + conftest）

| 项 | 内容 |
| --- | --- |
| 状态 | ✅ 已完成（2026-11-04，验证与交付记录见 `docs/plans/task-03-库与模型基座.md`） |
| 认领人 | A（数据基座） |
| 主责 | A（数据基座） |
| 前置 | `task-1` |
| 写作用域 | `docker-compose.yml`、`app/db.py`、`app/models/`、`alembic/versions/`、`tests/conftest.py` |

> 交付补充（2026-11-04）：实际产出比本卡多一项——`alembic/versions/0003_fix_day1_ck_names.py`，
> 用于 RENAME 修正 `0001` 落库时被 naming_convention 二次前缀的 4 个 CHECK 约束名（0001 本体未动，
> 依据 AGENTS §4.6）。conftest 未按下方「连 silvermate_test」执行，改为主库事务回滚隔离（决策记录在计划文档）。
> 本机 5432/6379 被 langfuse 占用时，`.env` 设 `POSTGRES_HOST_PORT`/`REDIS_HOST_PORT` 换端口（默认 5432/6379）。

## 为什么是它先做

这是全组前置——别人都在等你的 ORM 与 conftest。A 晚一小时，C 和 D 就整体后移一小时。

## 改哪些文件

- `docker-compose.yml`（**新增**，最小版：只 `postgres=pgvector/pgvector:pg16` + `redis:7`，端口 5432/6379，带健康检查；完整 7 服务版本留到 Day 10）
- `app/db.py`（async engine + `async_sessionmaker` + `get_session` 依赖；池参数取 `app/core/config.py` 的 `database_pool_*`；**导入不建连接**，保证无库也能起服务）
- `app/models/staff.py`、`app/models/guardian.py`、`app/models/relation.py`、`app/models/__init__.py`（导出，**A 独占**）
- `alembic/versions/0002_day2_identity.py`
- `tests/conftest.py`

## 字段口径（严格按技术设计文档）

- §3.2：`staff_accounts`、`guardian_accounts`（line 349-371）
- §3.3：`elder_staff_assignments`、`elder_guardians`、`staff_tokens`（line 375-406）

硬约束：

1. **枚举列一律 CHECK，禁用 PG ENUM**（技术设计文档 line 685）。
2. 唯一/部分唯一索引以 `docs/ER_数据模型设计.md` §5 为准；新增索引**先改 ER 再改迁移**。
3. `staff_tokens.token_hash` 是 `text PK`，存哈希不存明文。
4. `0002` 必须可回滚（`downgrade()` 完整）。
5. 不删改已合入的 `0001`（AGENTS §4.6）。

## conftest 要求

- 连 `silvermate_test` 库
- 每个测试一个事务、结束 `rollback`
- 无库时 **skip 并打印明确原因**，不许静默通过（否则覆盖率虚高）

## 验收命令

```bash
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
uv run pytest -q
# psql 里 \d staff_accounts，逐字段比对技术设计文档
```

## 禁止触碰

`app/api/**`、`app/core/security.py`、`app/core/auth.py`（B 的地盘）、`app/main.py`（E 独占）。

## 风险

`app/models/**` 与 `alembic/versions/**` 在 AGENTS §5 高风险清单里 —— **必须人工逐行读 diff 后合并**。

## 报告

diff 摘要 + 每张表与技术设计文档字段的逐字段对照结论。
