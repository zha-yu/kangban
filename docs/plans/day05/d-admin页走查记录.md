# Day 5 · dev-D admin 页走查记录（N5）

> 状态：已完成（真实端点走查）　｜　版本：v1.4　｜　日期：2026-11-07
> 关联：docs/plans/day05/d-双端前端推进计划.md（W1-d/e）、docs/plans/day05/d-DAG.md（N5 / G-C）
> 上游依据：身份契约 v1.8 §4 E12/E15/E13/E18/E16/E17、§6 权限矩阵、§7 错误码、S-24｜PRD FR-ADM-01（435）
> 变更记录见文末

---

## 1. 走查环境（已解除阻塞）

| 项 | 实测结果 |
| --- | --- |
| Docker Desktop | 由用户目录安装实例启动，daemon 就绪 |
| `silvermate-postgres` | `pgvector/pgvector:pg16`，Up (healthy)，0.0.0.0:5432 |
| `silvermate-redis` | `redis:7`，Up (healthy)，0.0.0.0:6379 |
| 迁移 | `uv run alembic upgrade head` 成功（本次执行 0004→0005） |
| 后端 | `uv run uvicorn app.main:app --port 8000`；`GET /health` 200，`X-Trace-Id` 存在 |
| 前端 | `pnpm --dir frontend dev`，`http://127.0.0.1:5173/console.html` 200 |
| 浏览器 | 真实 Chromium 会话；截图存外部目录，未入库 |

测试账号（本地教学数据，非真实长者/员工）：admin 王管理（137****0000，CLI 引导）、care_worker 界面护工（137****0003，UI 创建）、doctor 测试医生（137****0004，E12 API 创建）、guardian 界面家属（139****0006，UI 创建并绑定）。口令仅在本次本地会话使用，未写入仓库文档。

> 环境备注：doctor 首次创建时 PowerShell 请求编码把中文姓名写成 `????`，已在本地测试库修正为「测试医生」并重新登录取证；属测试脚本编码问题，与前端无关。

## 2. 四流程走查（全部通过）

| # | 流程 | 端点与实际状态码 | 请求要点 | 页面结果 | 落库回查 |
| --- | --- | --- | --- | --- | --- |
| 1 | 建员工 | `POST /api/staff`（E12）→ **201** | `{name:界面护工, role:care_worker, phone:13700000003, password, elder_ids:[张爷爷]}` | 成功提示：「已创建护工：界面护工（137****0003）。本次分配长者：1 位。」`employee-created-success.png` | `staff_accounts` 新增 care_worker；`elder_staff_assignments` 写入 1 条 |
| 2 | 建家属 | `POST /api/guardians`（E15）→ **201** | `{name:界面家属, phone:13900000006, password}` | 成功提示含 `139****0006` 与 `guardian_id`；绑定表单自动填入该 ID | `guardian_accounts` 新增 active 家属 |
| 3 | 绑定长者 | `POST /api/elders/{elder_id}/guardians`（E13）→ **201** | `{guardian_id:自动填入, relation:女儿}` | 成功提示含 elder_id / guardian_id / 关系「女儿」/ 状态「生效中」 | `elder_guardians` 新增 `(elder, guardian, 女儿, active)` |
| 4 | 设备建档 | `GET /api/rooms`（E18）→ **200**；`POST /api/devices`（E16）→ **201**；`GET /api/devices`（E17）→ **200** | `{room_id:101 房}` | 密钥卡片「设备密钥（仅本次展示）」；点「我已记录，关闭」后显示「设备密钥已关闭，无法再次查看」；设备列表新增未绑定设备 | `devices` 新增 `room_id=101、elder_id=NULL、offline` |

截图路径（外部，未入库）：

| 流程 | 文件 |
| --- | --- |
| admin 登录 / 侧栏 | `C:\Users\Alienware\AppData\Local\Temp\trae\screenshots\login-success-console.png`、`fullpage-after-login.png` |
| 建员工成功 | `...\employee-created-success.png` |
| 建家属成功 | `...\guardian-account-created-success.png` |
| 绑定成功 | `...\elder-bound-success.png` |
| 密钥关闭态 + 设备列表 | `...\device-key-closed-and-list.png` |

> 一次性密钥：含明文密钥的截图已在取证后删除，未保留在任何持久化位置；仅保留「已关闭，无法再次查看」态截图。密钥未写 localStorage、未打印日志。

## 3. 越权反向验证（3/3 被拒）

| # | 角色 | E12 | E15 | E13 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 1 | care_worker | **403** `ROLE_FORBIDDEN` | **403** `ROLE_FORBIDDEN` | **403** `ROLE_FORBIDDEN` | 通过 |
| 2 | doctor | **403** `ROLE_FORBIDDEN` | **403** `ROLE_FORBIDDEN` | **403** `ROLE_FORBIDDEN` | 通过 |
| 3 | guardian | **401** `TOKEN_INVALID` | **401** `TOKEN_INVALID` | **401** `TOKEN_INVALID` | 通过（见下方口径说明） |

**口径说明（只登记，不修改后端）**：任务卡与计划原文写「三种角色均 403」，但身份契约 **S-24** 明定「主体种类与端点不符 → **401**，不是 403」：`require_roles` 先走 `current_staff`，guardian 凭证种类不符即判 `TOKEN_INVALID`。实测与冻结契约一致，故按 401 记录；建议后续把任务卡文案改为「staff 角色 403 / guardian 401」。

前端无白屏补充验证：care_worker 登录 console 正常渲染「工单看板」、doctor 正常渲染「复核队列」，两者侧栏均无「用户与角色」「模拟设备」；guardian 登录 family 正常渲染日报页。截图：`care_worker_login.png`、`doctor_relogin_test.png`、`family-after-login.png`。

## 4. 页面级 403 文案（K2-B）核对

- `admin-api.ts` 按状态码映射固定文案：403「当前账号无权执行该操作」、409「手机号已被占用」/「已绑定或已满 2 位」、404「目标不存在」、422「提交内容不符合要求」；不解析 `trace_id`、不回显 `detail` 原文。
- 非 admin 无法进入 admin 页（`App.tsx` 仅 `role=admin` 渲染 `AdminView`），故 UI 层未制造 403 场景；403 映射由代码级核对 + 上述 API 实测共同证明。

## 5. 目标验收项

| 项 | 结果 |
| --- | --- |
| admin 可见「用户与角色」「模拟设备」且可进入 | 通过（截图 + 侧栏实测） |
| 建员工 / 建家属 / 绑定长者 / 设备建档四流程各走通 1 次 | 4/4 通过（含 DB 回查） |
| 非 admin 访问 E12/E15/E13 被拒 | 3/3 通过（2×403 + guardian 401 per S-24） |
| 非 admin 前端无白屏 | 通过（care_worker / doctor / guardian 各 1 次） |
| 一次性 `device_secret` 仅展示一次 | 通过（关闭后不可见；明文截图已删除） |
| 样式合规（merge 后 CSS 口径） | 通过 | console 全局控件统一 **40px**（2026-11-07 远端裁决：桌面鼠标端；48px 仅 pad 长者端）；admin 勾选 40px；表格行高 41px/字体 14px；卡片标题 18px/说明 13px；admin CSS 圆角全部 `var(--radius-control)`。合并后 `console/index.css` 未定义 token=0、硬编码 hex=0。先前 48px 渲染实测作废（merge 前口径） |
| 构建与包隔离 | `pnpm --dir frontend build` 通过；console/family 产物 three-vrm 0 命中 |

## 6. 录屏

- **未录制可用视频**：浏览器自动化工具仅能截图；另尝试无头 Chrome CDP 抓帧写 MJPEG-AVI，但 headless 下自动化表单重复提交（首请求 201、次请求 409）且环境无 ffmpeg/编码器，未产出可用视频。未伪造路径、未入库媒体。
- 以 10 张外部截图作为走查证据（路径见 §2/§3），未入库。
- M5.4 演示若必须视频，需人工在可见浏览器窗口录屏；admin 段操作步骤与本记录一致。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 记录 N5 阻塞（后端/DB/Docker 不可用），四流程与越权行均未执行 |
| v1.1 | 2026-11-07 | 环境解除（Docker Desktop + postgres/redis + uvicorn）后完成真实走查：四流程 4/4、落库回查 4/4、越权 3/3（guardian 按 S-24 为 401）、非 admin 无白屏 3/3；一次性密钥关闭态取证，明文截图删除；录屏未录制（工具限制）。未伪造结果 |
| v1.2 | 2026-11-07 | 补录屏尝试结论：无头 Chrome CDP 抓帧写 MJPEG-AVI 因 headless 表单重复提交 + 无编码器失败，未产出视频；走查截图证据不变 |
| v1.3 | 2026-11-07 | 补 admin 页真实 DOM 尺寸实测（1280×800）：输入框/主按钮/勾选 48px、表格行高 41px、正文 14–16px、无横向溢出；顶栏退出为既有 40px，不属本次新增 |
| v1.4 | 2026-11-07 | merge origin/develop 后按远端口径将 admin 控件对齐 40px / `--radius-control`；原 48px 实测作废；合并后 console/index.css 未定义 token=0、hex=0；构建通过 |
