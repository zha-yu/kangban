> 状态：已冻结　｜　版本：v1.2　｜　日期：2026-11-04
> 关联：docs/team_tasks/task-3-库与模型基座.md
> 上游依据：PRD §3.3/§4.7/AC-24（业务规则）｜技术设计文档 §3.2/§3.3/line 685（实现口径）｜ER_数据模型设计.md §4.1.6/§4.1.7/§4.2/§5（字段+索引）
> 变更记录见文末

---

## 为什么现在做

Day 2 前置——task-1 已合入 5 张骨架表（rooms/beds/devices/elders/personas），本计划补齐身份体系第二梯队 5 张表。下游 task-4（鉴权内核）、task-5（登录接口）、task-6（长者 CRUD）全部依赖本批 ORM 与 conftest，晚合入一小时，下游整体后移一小时。

## 改哪些文件（8 项）

| # | 文件 | 操作 | 要点 |
| --- | --- | --- | --- |
| 1 | `docker-compose.yml` | 新建 | 最小版：`pgvector/pgvector:pg16` + `redis:7`，端口 5432/6379，带健康检查；完整 7 服务留 Day 10 |
| 2 | `app/db.py` | 新建 | async engine + async_sessionmaker + `get_session` 依赖；池参数取 `database_pool_*`；**导入不建连接** |
| 3 | `app/models/staff.py` | 新建 | `StaffAccount` + `StaffToken` |
| 4 | `app/models/guardian.py` | 新建 | `GuardianAccount` |
| 5 | `app/models/relation.py` | 新建 | `ElderStaffAssignment` + `ElderGuardian` |
| 6 | `app/models/__init__.py` | 修改 | 追加导出；保留「C/D 组模型补齐前禁跑 autogenerate」警告 |
| 7 | `alembic/versions/0002_day2_identity.py` | 新建 | 手写迁移，`down_revision="0001_day1"`，downgrade 完整 |
| 8 | `tests/conftest.py` | 新建 | 每测试一事务 + rollback；无库 skip 并打印原因 |

写作域（A 独占）：`app/models/**`、`alembic/versions/**`。禁止触碰：`app/api/**`、`app/core/security.py`、`app/core/auth.py`（B）、`app/main.py`（E）、`0001` 迁移（AGENTS §4.6）。

## 5 张表字段口径（ER §4.1.6/§4.1.7/§4.2，与 TD §3.2/§3.3 一致）

- **staff_accounts**：staff_id uuid PK / name / role（CHECK admin|care_worker|doctor）/ phone? / password_hash / token_version（默认 0）/ status（CHECK active|disabled，默认 active）
- **guardian_accounts**：guardian_id uuid PK / name / phone? / webpush_subscription jsonb?（B5：P0 只存不真推）/ password_hash / token_version / status（同上）
- **elder_staff_assignments**：id uuid PK / elder_id FK→elders / staff_id FK→staff_accounts / relation（CHECK primary_care|doctor）/ assigned_at timestamptz；UNIQUE(elder_id, staff_id, relation)
- **elder_guardians**：id uuid PK / elder_id FK→elders / guardian_id FK→guardian_accounts / relation（自由文本，绑定级）/ status（CHECK active|unbound）/ assigned_at；UNIQUE(elder_id, guardian_id)；索引 (guardian_id, status)
- **staff_tokens**：token_hash **text PK**（只存哈希）/ staff_id FK→staff_accounts / device_id FK→devices / scope / expires_at timestamptz

## 硬约束

1. 枚举一律 CHECK，禁用 PG ENUM（TD line 685，裁决 C6）。
2. 唯一/查询索引以 ER §5 为准；新增索引先改 ER 再改迁移。
3. `staff_tokens.token_hash` 是 text PK，存哈希不存明文。
4. `0002` 可回滚（downgrade 完整，逆序 drop）。
5. 不删改 `0001_day1_identity_skeleton.py`。
6. 约束命名对齐 `Base.naming_convention`：target_metadata 挂载后约定会作用于迁移内的显式名——**迁移与 ORM 的 CHECK 都只给短名**（`name="role"/"status"/"relation"`），由模板拼出 `ck_staff_accounts_role` 等全名，两侧严格一致。⚠️ 0001 的 CHECK 按全名书写被二次前缀（`ck_rooms_ck_rooms_status`），属既有瑕疵，0001 已冻结不改，功能不受影响；后续如需修正走新迁移 `ALTER CONSTRAINT RENAME`。
7. 「每位长者 active 监护人 ≤2」是服务层校验（裁决 C4），不落 DDL。
8. `app/db.py` 导入不建连接（`create_async_engine` 惰性，首次使用才连）。

## 已拍板决策

| 问题 | 选择 | 理由 |
| --- | --- | --- |
| conftest 连哪个库 | 主库 `silvermate` rollback 隔离 | 教学规模不需要主/测库分离；避免再起一套 |
| 手写还是 autogenerate | 手写 | 与 0001 风格一致；C/D 组模型未齐，autogenerate 会误报 drop |
| docker-compose.yml 现在建吗 | 现在建最小版 | 下游 `docker compose up -d` 一键起依赖 |

## 步骤与验收

1. `docker compose up -d postgres redis` → 两容器 healthy。
2. 写 db.py / 模型 / 迁移 / conftest。
3. 验收命令：

```bash
uv run alembic upgrade head
uv run alembic downgrade -1 && uv run alembic upgrade head   # 回滚可逆
uv run pytest -q                                             # task-1 回归继续过
uv run ruff check app tests && uv run ruff format --check app tests
psql "$DATABASE_URL" -c "\d staff_accounts"   # 逐字段比对 ER §4.1.6
```

4. 人工审查（AGENTS §5）：`0002` 与 `app/models/**` 逐行读 diff 后合并。
5. 报告：diff 摘要 + 每张表与 ER 字段的逐字段对照结论。

## 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-11-04 | v1.0 | 初始草稿：基于 task-3 卡，ER/TD/PRD 三份文档对齐后的实施计划 |
| 2026-11-04 | v1.1 | 实施完成；发现并修正 CHECK 命名策略（约定模板会二次前缀显式全名，迁移与 ORM 一律改短名）；登记 0001 同源既有瑕疵；compose 端口变量化（本机 5432/6379 被 langfuse 占用，映射 15432/16379） |
| 2026-11-04 | v1.2 | 新增 `0003_fix_day1_ck_names.py`：把 0001 落库的 4 个双前缀 CHECK（`ck_rooms_ck_rooms_status` 等）RENAME 为约定产出名，可逆；0001 本体按 AGENTS §4.6 不动 |
