# Day 7 · dev-D 联调记录（d-联调记录）

> 状态：进行中　｜　记录人：dev-D（AI 协作）　｜　日期：2026-11-10
> 关联：`docs/plans/day07/d-推进计划-pad可靠性改绑联调.md`（计划）｜`docs/team_tasks/day07/dev-d-day07.md`（任务卡）
> 用途：D1 / D2 / D3 各项验证证据与问题登记；D3 收口后作为 G5 门禁证据链一部分。

---

## D2 · console 改绑分页与错误 UX（S2.1–S2.3）

### 代码修改

| 项 | 文件 | 内容 |
| --- | --- | --- |
| P2-25 | `frontend/src/console/pages/AdminUsersPage.tsx` | `fetchAllElders()` 完整分页（page_size=100 循环取满 total）；保存集合 = 勾选 ∪ 未进候选列表的已绑长者；「已选择 N 位 / 共 M 位」显示；createStaff 成功后刷新员工列表 |
| P2-40 | 同上 | `refreshStaffList` 请求序号防旧响应覆盖 |
| P2-26 | `frontend/src/console/admin-api.ts` | 401 →「登录已过期，请重新登录」；400/422 解析 `detail` 提示具体原因；409 保留编辑态（既有实现核对） |

构建门禁：`pnpm --dir frontend build` 零 TS 错误，pad/console/family 三产物均出（S2.1、S2.2 各跑一次）。

### 造数验证（2026-11-10，后端 localhost:8000，管理员 13700000001）

一次性脚本（/tmp/d2_seed.py、/tmp/d2_reassign.py、/tmp/d2_reassign2.py，不入 Git）经 API 执行：

| # | 验证项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | E5 建 150 位压测长者 | ✅ created=150 failed=0 | `D2压测长者001–150`，E7 total=153 |
| 2 | E7 分页：第 101 位起可见 | ✅ | page1=100 条 / page2=53 条；`D2压测长者101` 在 page2 中找到并成功用于绑定 |
| 3 | E20 差集保存（新增） | ✅ 200 | `added=[压测002 elder_id]`、`removed=[]`（001 未绑入，diff 正确） |
| 4 | E20 差集保存（移除） | ✅ 200 | 共同护工兜底下 `removed=[种子长者 elder_id]`、`added=[]` |
| 5 | E20 409 拦截 | ✅ 409 | 唯一责任人移除 → `{"detail":"长者 00326698-… 移除后将失去全部责任护工","code":"CONFLICT"}`（两条，trace_id 在案） |
| 6 | 连续 5 次快速刷新一致 | ✅ | 5 次快照完全一致，bound=3 |
| 7 | createStaff 后立即出现 | ✅ | 201 建号后按 staff_id 立即在 GET /api/staff 命中 |
| 8 | 401（伪 token） | ✅ 401 | GET /api/staff 与 PUT reassign 均 `TOKEN_INVALID`；前端映射「登录已过期，请重新登录」（S2.2 代码核对） |

### 遗留与登记

- UI 层（勾选「已选 N / 共 M」显示、409 弹窗文案与编辑态保留）待 D3 联调用浏览器核验；API 层证据已齐。
- 测试数据留存：150 位 `D2压测长者*`、4 名测试员工（D2测试护工 / D2共同护工×2 / 及 1380000990x 手机号），带显式前缀便于识别与后续清理；E5 无删除端点，未直插 SQL 清理。
- `console/api.ts` 保持零改动（K2 方案 B 约束未破坏）。

---

## D1 · pad 播放/打断/VAD 可靠性

（待 S1.1–S1.5 执行后回填）

---

## D3 · 触达/路由联调

（待 D3 执行后回填；含依赖状态检查记录与 SQL 核对证据）
