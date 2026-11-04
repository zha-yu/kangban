# task-6 · Step4 长者档案 CRUD + 设备绑定第一层 + 范围过滤

| 项 | 内容 |
| --- | --- |
| 状态 | ✅ 已完成（2026-11-04；router 注册与当日总验收归 `task-7`） |
| 认领人 | — |
| 主责 | D（长者与设备） |
| 前置 | `task-3`、`task-4` |
| 写作用域 | `app/services/elder_service.py`、`app/services/device_service.py`、`app/api/elders.py`、`app/api/devices.py`、`tests/test_elders_api.py` |

> **工作量最大的一张卡**，建议给最熟 SQLAlchemy 的人。

## 必须实现

端点以 `docs/接口契约_身份与权限.md` 为准：

1. **长者档案 CRUD** —— 管理员可写；护工/医生/家属只读自己范围
2. **设备绑定第一层** —— 写 `devices.elder_id` 建立 `device → elder` 1:1；解绑置空并把旧 token 作废
3. **双人间候选列表** —— `GET /devices/{id}/elder-candidates`，字段按裁决 D3 脱敏

## 范围过滤硬口径（PRD §3.1 + FR-ADM-01/03）

- 护工/医生：`WHERE elder_id IN (SELECT elder_id FROM elder_staff_assignments WHERE staff_id = :me)`
- 家属：`WHERE guardian_id = :me AND status = 'active'`（走 `elder_guardians`）
- **范围过滤只能在 service 层做**；api 层只把 `current_user` 传下去；前端隐藏按钮不算数（AC-18）

## 验收

1. 非管理员 `POST /elders` → 403
2. 护工请求非负责长者详情 → 403（AC-18）
3. 每条写操作测试都「回查 DB」断言字段真的落库（大纲 2.3 口径：接口 200 ≠ 任务完成）
4. 第二台设备绑同一长者 → 409/422
   - `uq_devices_elder_id` 部分唯一索引已存在于 `alembic/versions/0001_day1_identity_skeleton.py:115`，不用新建
5. 至少 3 条 pytest

## 禁止触碰

`app/models/**`、`alembic/**`（要加索引先改 ER §5 再找 A 加迁移）、`app/core/**`、`app/main.py`。

## 报告

diff 摘要 + 五条验收的断言位置。
