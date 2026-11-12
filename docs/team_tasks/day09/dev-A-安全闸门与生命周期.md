> 状态：未开始　｜　版本：v1.2　｜　日期：2026-11-12
> 负责人：**dev-A（唯一）**　｜　任务包：**A · 安全闸门与数据生命周期**　｜　容量：**19h ＝ 2.375 人日**
> 关联：docs/team_tasks/day09/day09-任务总览与DAG.md v1.0（§4.1 分工、§5 对接点、§6 写域）｜docs/team_tasks/day09/contracts.md（X2/X4/X8/X9 消费与提供）
> 上游依据：PRD line 440（FR-ADM-06 三道闸门）、line 436（FR-ADM-02 教学重置）、line 443（FR-ADM-09 退房/away）、line 439（FR-ADM-05 日志导出）、line 500/506/511/518/521（AC-18/24/29/36/39）、line 320–325（用药终态，X8 依据）｜技术设计文档 line 1169（Day 9 落地清单）、line 1172–1174（功能/技术验收/风险）、line 778（迁移批次，本包新增 0020 须修订留痕）
> 遗留承接：docs/plans/day08/adhoc-20261112-审查缺口修复.md 的 **F10（迁移部分）、F17（枚举部分）**；Day 9 原生 **三道闸门 / 日志脱敏 / 审计导出 / 教学重置 / 退房 / away / SECURITY.md**
> 变更记录见文末

---

# dev-A · Day 9 任务书 · 安全闸门与数据生命周期

## 0. 你的边界（先读这条）

### 0.1 写域仅限（越界即返工）

```
alembic/versions/0020_*.py            （新增，本包唯一迁移）
app/core/redact.py                    （新增）
app/core/logging.py                   （增量：接集中脱敏）
app/core/runtime_context.py           （增量：会话闸门加固）
app/services/lifecycle_service.py     （新增）
app/services/audit_log_service.py     （增量）
app/services/auth_service.py          （增量：token 吊销）
app/services/voiceprint_service.py    （增量：退房清声纹）
app/services/device_service.py        （增量：退房解绑）
app/api/lifecycle.py                  （新增）
app/api/audit_export.py               （新增）
app/api/__init__.py                   （仅末尾追加 2 行 ROUTERS）
tests/test_gates.py                   （新增）
tests/test_lifecycle*.py              （新增）
docs/SECURITY.md                      （新增）
```

### 0.2 禁触（他人写域，需改动时提需求不直接改）

`app/graphs/**`、`app/scheduler/**`、`app/agents/**`、`app/router/**`、`app/main.py`（**dev-B 独占装配**）、`app/services/memory_service.py`（B）、`health_service.py`／`notify_service.py`／`report_service.py`／`media_service.py`／`persona_service.py`（C）、`app/core/context.py`（C）、`frontend/**`（C/D）、`tests/eval_gate.py`（D）。

需 B 装调度器/改装配、需 C 提供人设或通知出口时：**提改动需求给持锁人**，在 §5 执行记录登记等待项，不自己改。

### 0.3 高风险文件（AGENTS §5：先出方案再落代码＋人工逐行读 diff）

`alembic/versions/0020_*`、`app/core/runtime_context.py`、`app/services/auth_service.py`。这三处**必须先出方案**（写在本文件 §5 或对应计划件）经确认后才落代码。

### 0.4 角色口径

本包**不含**独立测试/产品/设计角色工作。`tests/test_gates.py` 与攻击用例是**你作为安全域开发的内嵌交付物**（TD line 1169 明列该文件为 Day 9 落地项，day08 总览同口径）；`docs/SECURITY.md` 是 TD line 1169 明列的实现口径文档，由承接闸门的开发自产。

---

## 1. 节点 A1 · 迁移 0020 ＋ 脱敏集中化（4h）

**前置**：N0 契约冻结（D9 09:30）。**无其他前置，D9 上午第一个开工。**
**对接**：**提供 X8**（迁移 0020）给 B4/D2；**消费 X9**（日志脱敏判据）。

### 1.1 现状实测（2026-11-12 取证）

- **无 `app/core/redact.py`**。脱敏分散 4 处，违反 AGENTS §8「脱敏工具集中放 `app/core/`」：
  - `app/services/auth_service.py:147 def _mask_phone`
  - `app/services/staff_service.py:87 def mask_phone`
  - `app/services/device_service.py:153 def _mask_display_name`
  - `app/services/decision_log_service.py:47 def redact_input`
- `app/models/medication.py:129-137`：`terminal_state` CHECK 仅 4 值（**无 `late_taken_reported`**）；`failure_reason` CHECK 仅 `('no_answer','denied','tts_failed','offline')`（**无 `rtc_failed`**）。
- TD line 778 记「Day 9 迁移 0 张」——本包新增 0020 与之冲突，**须同提交修订留痕**。

### 1.2 要做什么

1. **迁移 0020（1h，优先交付，B4/D2 在等）**：按 contracts.md **X8** 扩两个 CHECK 至 5 值。只新增不改历史；upgrade/downgrade 均可逆；不改既有行数据。
2. **`app/core/redact.py`（2h）**：集中脱敏工具，至少覆盖——手机号、身份证号、密钥/API Key、完整住址、姓名按需。收编上述 4 处实现为**调用集中函数**（旧函数体删除或改为薄转发，**不保留第二套实现**，AGENTS §4.8）。
3. **日志全链路接线（1h）**：`app/core/logging.py` 的 formatter/filter 接 `redact`，确保**每一行日志**（含 `trace_id`）输出前脱敏；覆盖异常栈与 `extra` 字段。
4. **TD 修订留痕**：同提交在技术设计文档文末「修订记录」新增一条，写明「Day 9 迁移由 0 张改 1 张（0020），依据 PRD line 320-325／336 需扩 medication 两枚举」。

### 1.3 交付物清单

| # | 交付物 | 形态 |
| --- | --- | --- |
| 1 | `alembic/versions/0020_day9_medication_enums.py` | 可逆迁移 |
| 2 | `app/core/redact.py` | 集中脱敏模块＋单测 |
| 3 | `app/core/logging.py` 接线 | 增量 diff |
| 4 | 4 处旧脱敏实现收编 | 增量 diff（4 文件） |
| 5 | TD 文末修订记录新增一条 | 文档 diff（同提交） |
| 6 | 日志脱敏验证样本与 grep 结果 | 证据，回填 §5 |

### 1.4 量化验收（逐条可执行）

```bash
# ① 迁移可逆
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
uv run alembic current && uv run alembic heads   # 单头，无多头

# ② 枚举已扩（DB 侧断言）
#   terminal_state 接受 'late_taken_reported'；failure_reason 接受 'rtc_failed'
#   写入非法值仍被 CHECK 拒绝

# ③ 脱敏已集中（旧实现零残留）
grep -rn "def _mask_phone\|def mask_phone\|def _mask_display_name\|def redact_input" app/services/
#   期望：零命中（全部收编到 app/core/redact.py）

# ④ 日志无明文敏感字段（AC-36 后半）
#   跑一遍含手机号/身份证/密钥的请求，取日志样本：
grep -E "1[3-9][0-9]{9}" <日志样本>            # 期望零命中
grep -E "[1-9][0-9]{5}(19|20)[0-9]{2}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])[0-9]{3}[0-9Xx]" <日志样本>   # 期望零命中
grep -iE "sk-[A-Za-z0-9]{16,}\|api[_-]?key\s*[:=]\s*\S{12,}" <日志样本>   # 期望零命中

# ⑤ 每行日志仍带 trace_id（不回退）
uv run pytest tests/ -q -k "trace or redact or logging"
```

**通过判据**：①–⑤ 全部满足；`ruff check`＋`ruff format --check` 全绿。

### 1.5 禁触与红线

不改历史迁移；不硬编码任何密钥（AGENTS §4.2）；脱敏**不得**把 `trace_id` 一起抹掉；不收编出第二套脱敏实现。

**CP-A1**：报告 5 条命令原文输出＋迁移号＋TD 修订记录位置。**A1 的迁移部分须 D9 12:00 前交付**（B4/D2 在等 X8）。

---

## 2. 节点 A2 · 三道隔离闸门 ＋ SECURITY.md（6h）

**前置**：A1（审计断言需先有集中脱敏）。**软依赖**：B1（工具闸门加固）、B2（记忆召回隔离）——**不等待**，先对现有实现写攻击用例。
**对接**：**消费 X9**（覆盖率判据）；与 B1/B2 互为「攻击面 ↔ 加固」关系。

### 2.1 现状实测

- `tests/test_gates.py` **不存在**；`tests/` 现只有 `eval_gate.py`（无 `test_injection.py`，该文件归 B4）。
- 三道闸门靶点：① 会话 `thread_id`（`app/core/runtime_context.py`）② 记忆检索（`app/services/memory_service.py:517 ORDER BY importance DESC`，**embedding 恒 None、召回用 difflib**，B2 在修）③ 工具身份（`app/services/tool_executor.py`，B1 在修）。
- `audit_log_service.record_audit` **已存在**（0007 提前落地），可直接用于断言。

### 2.2 要做什么

1. **会话闸门加固**：`thread_id` 按 `(elder_id, session_seq)` **由服务端生成**；校验请求携带的 `thread_id` 归属当前身份，伪造/跨长者 → **403＋审计**。
2. **`tests/test_gates.py`**：三类越权 × 正反用例，**每条断言必须人读**（TD line 1174 风险原文：「AI 写的权限测试容易『测了个寂寞』」）。每条越权用例须断言**三件事**：HTTP 状态码/拒绝结果、`audit_logs` 新增行数、`trace_id` 存在。
3. **覆盖率**：核心 Tool 与权限场景 **100%**（TD line 1173）。
4. **`docs/SECURITY.md`**：威胁模型／降级矩阵／数据保留／HITL 清单四节（TD line 1169 明列）。

### 2.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `app/core/runtime_context.py` 会话闸门增量 |
| 2 | `tests/test_gates.py`（三类越权 × 正反，含审计断言） |
| 3 | `docs/SECURITY.md`（四节齐） |
| 4 | 覆盖率报告（`pytest --cov` 输出，核心 Tool/权限 100%） |
| 5 | 逐条断言的人读签认记录（回填 §5） |

### 2.4 量化验收

| 攻击用例 | 期望 | 依据 |
| --- | --- | --- |
| 长者 A 会话中伪造长者 B 的 `thread_id` | **403** ＋ `audit_logs` **+1 行** ＋ 有 `trace_id` | AC-39①／FR-ADM-06① |
| 直接向量检索 B 的记忆（绕过 API 直调 service） | **拒绝**（强制 `WHERE elder_id`）＋审计 | AC-39②／FR-ADM-06② |
| 以 B 的 `elder_id` 调 MCP 工具 | **拒绝** ＋审计＋`trace_id` | AC-39③／AC-29／FR-ADM-06③ |
| 护工 A 请求不负责长者 B 的详情/日报 | **403＋审计**（前端隐藏按钮不算） | AC-18 |
| 家属 C 请求未绑定长者 B 的日报 | **403＋审计** | AC-18 |
| 停用/解绑后旧凭证（护工/家属/设备三类各一） | **立即无法通过校验**，审计保留 | AC-24 |
| 正常路径（本人访问本人） | **全绿**，不误拦 | TD:1173 |

```bash
uv run pytest tests/test_gates.py -q                      # 越权用例全被拦＝测试全绿
uv run pytest --cov=app --cov-report=term-missing -q       # 核心 Tool/权限场景 100%
```

**通过判据**：7 类用例全部满足；覆盖率报告核心 Tool 与权限场景 **100%**；`docs/SECURITY.md` 四节齐。

**注意**：若 B1/B2 加固未完成导致②③用例红，**这是发现漏洞不是你的失败**——登记漏洞给 B，待其加固后复跑转绿。**不得为了让测试变绿而放宽断言**。

**CP-A2**：报告 7 类用例实测表＋覆盖率数字＋漏洞登记清单。

---

## 3. 节点 A3 · 生命周期服务（6h：骨架 4h＋联调 2h）

**前置**：A1（退房健康数据脱敏归档复用 `redact.py`）。**软依赖**：B2 的 **X4**＋B/C 的 **X12** 域清理钩子——**骨架先行**，先用桩，D9 下午签名冻结后 D10 16:00 联调。
**对接**：**消费 X4/X12**（B 提供记忆与用药清理、C 提供健康/通知/日报/工单清理）；**提供**生命周期 service 签名给 A4。

> **责任边界（X12）**：业务表分属 B/C 写域，**你不直写他人表**（AGENTS §2.1 唯一写库层）。你只做**编排＋审计＋自有三项清理**（`auth_service.revoke_all_tokens`／`voiceprint_service.purge_elder_samples`／`device_service.unbind_all`），其余调各域 `purge_elder_data` 钩子。

### 3.1 现状实测

`app/services/lifecycle_service.py` **不存在**。console 侧「教学数据重置 / 人设配置 / 审计日志」均置灰（`frontend/src/console/App.tsx:34`、`layout/AppLayout.tsx:8,39`）。

### 3.2 要做什么（三条流水线，全程审计）

**① 教学周期重置（FR-ADM-02／AC-19，P0）**
- **二次确认**：无确认令牌 → **409**，不执行任何删除。
- 清除：业务数据（健康记录/用药事件/工单/通知/日报）＋记忆索引——**全部调 X12/X4 钩子**，各域自己删自己的表。
- **保留**：账号／角色／配置／**重置审计**。
- 重置后业务记录与记忆索引**均不可检索**。

**② 退房清数据（FR-ADM-09，P1）**
- 流水线顺序（**每步 1 条审计**）：吊销全部 token → 删声纹样本与向量 → 清长期记忆（X4）→ 健康数据**脱敏归档**（调 C 的 `health_service.purge_elder_data(reason=checkout)`，内部用 A1 `redact.py`，不物理删业务行）→ 设备解绑。**每步独立可重入**，中途失败可单独重跑，不回滚已吊销 token。
- 完成后：全部 token **401**、声纹与长期记忆**不可检索**。

**③ away 暂离（TD:1169）**
- 暂停采集与提醒（**不改药单/计划本身**，只暂停触达）；日报改为**暂离告知**。
- 恢复后按原计划继续。

### 3.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `app/services/lifecycle_service.py`（三条流水线＋审计） |
| 2 | `app/services/auth_service.py` 吊销增量（`revoke_all_tokens`） |
| 3 | `app/services/voiceprint_service.py` 清理增量＋`device_service.py` 解绑增量 |
| 4 | `tests/test_lifecycle*.py`（三条流水线 × 正反 × 审计行数断言） |
| 5 | X4 联调证据（与 B2） |

### 3.4 量化验收

| 场景 | 期望 | 依据 |
| --- | --- | --- |
| 重置无二次确认令牌 | **409**，零删除、零审计外的副作用 | FR-ADM-02 |
| 重置带确认令牌 | 业务记录查询 **0 行**、记忆检索 **0 条**；账号/角色/配置**仍在**；`audit_logs` 有重置记录 | AC-19 |
| 退房流水线完成 | 旧 token 请求 **401**；声纹查询 **不可检索**；记忆检索 **0 条**；**每步 1 条审计**（步数＝审计行数） | FR-ADM-09／TD:1172 |
| **退房→pad 端到端（联调）** | 退房完成后 pad 持旧设备 token 请求 → **401**，且页面落**回登录/未授权态**（pad 既有 401 统一口径，`pad/api.ts:58-63`）。后端断言 401 由 A 负责；**前端表现由 D 在 M5.4 联调确认**，A 不改 pad | FR-ADM-09／AC-24 |
| **退房→family 端到端（联调）** | 退房完成后原绑定家属查该长者日报 → **403**，且显示「无权查看该长者的数据」/「未绑定长者」空态（`family/api.ts:265`、`HomePage.tsx:91`）。后端断言 403＋审计由 A 负责；**前端表现由 D 在 M5.4 联调确认**，A 不改 family | FR-ADM-09／FR-RPT-02 |
| 退房后健康数据 | **脱敏归档**（明文手机号/身份证 grep 零命中），业务行未物理删除 | PRD §1.5 |
| away 期间 | 采集与提醒**暂停**（到点不触达）；日报内容为**暂离告知** | TD:1169 |
| away 恢复 | 按原计划继续触达，药单未被修改 | TD:1169 |
| 非 admin 调三条流水线 | **403＋审计** | FR-ADM-02/09 |

```bash
uv run pytest tests/test_lifecycle*.py -q
grep -rn "session.add(\|from app.models" app/services/lifecycle_service.py   # 允许（services 是唯一写库层）
```

**CP-A3**：报告三条流水线的审计行数对照表＋401/409/403 实测＋X4 联调证据。

---

## 4. 节点 A4 · 生命周期 REST ＋ 审计导出 REST（3h）

**前置**：A3。**对接**：**提供**端点给 C6（console 管理台）；**消费 X9**（错误体与审计口径）。

### 4.1 要做什么

1. **`app/api/lifecycle.py`**：重置／退房／away 三个端点，**admin-only**，重置与退房需**二次确认令牌**。薄路由：只校验＋调 service＋序列化，**不写业务逻辑**（AGENTS §2.3）。
2. **`app/api/audit_export.py`**：操作日志导出 **CSV**，支持**按时间与操作人筛选**（FR-ADM-05，P2）。
3. **`app/api/__init__.py`**：末尾**追加 2 行** ROUTERS（只增不改，沿用该文件既有约定）。

### 4.2 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `app/api/lifecycle.py`（3 端点） |
| 2 | `app/api/audit_export.py`（CSV 导出） |
| 3 | `app/api/__init__.py` 追加 2 行 |
| 4 | 接口测试（权限 × 筛选 × 二次确认） |
| 5 | 端点契约回填 contracts.md（若 N0 未覆盖字段细节） |

### 4.3 量化验收

| 场景 | 期望 |
| --- | --- |
| 非 admin 调任一端点 | **403＋审计** |
| 重置/退房无二次确认令牌 | **409** |
| CSV 导出 | 行数与看板查询**一致**；可按时间区间与操作人筛选；**不含 PII 明文**（手机号/身份证 grep 零命中） |
| CSV 内容一致性 | 抽 3 条与 `audit_logs` 表逐字段核对一致 |
| 路由注册 | `grep -c "router" app/api/__init__.py` 增 2；`main.py` **未被修改**（装配归 B） |

```bash
uv run pytest tests/test_api_lifecycle*.py tests/test_api_audit_export*.py -q
uv run ruff check app tests && uv run ruff format --check app tests
```

**CP-A4**：报告端点清单＋403/409 实测＋CSV 抽样核对结果。**A4 完成即 M5.4 的 A 侧判据达成。**

---

## 5. 执行记录与对接登记（待回填）

| 日期 | 节点 | 提交 | 验收结果 | 等待项/漏洞登记 |
| --- | --- | --- | --- | --- |
| — | A1 | — | — | — |
| — | A2 | — | — | — |
| — | A3 | — | — | — |
| — | A4 | — | — | — |

**对接登记**：

| 对接点 | 对方 | 我提供/消费 | 冻结时点 | 联调时点 | 状态 |
| --- | --- | --- | --- | --- | --- |
| X8 迁移 0020 | B4／D2 | 提供 | N0 | D9 12:00 | 待 |
| X4 记忆清理钩子 | B2 | 消费 | D9 下午 | D10 16:00 | 待 |
| X12 域清理钩子集 | B2（用药）／C2（健康/通知/日报/工单） | 消费 | D9 下午 | D10 16:00 | 待 |
| X9 门禁与脱敏判据 | D3 | 消费 | N0 | D11 16:00 | 待 |
| 生命周期/审计端点 | C6 | 提供 | N0 | D11 15:00 | 待 |
| 装配需求（如需） | B（main.py 持锁） | 提需求 | — | — | 待 |

**高风险文件方案登记**（A1 迁移／A2 runtime_context／A3 auth_service，落代码前必填）：待填。

---

## 6. 风险与回退

| # | 风险 | 处置 |
| --- | --- | --- |
| R-A1 | 收编脱敏后旧调用方行为变化（掩码格式不一致）导致既有测试红 | 以集中函数为唯一口径，改测试期望值；**不保留两套实现** |
| R-A2 | 闸门②③用例因 B 未加固而红 | 登记漏洞给 B，**不放宽断言**；M5.1 前复跑 |
| R-A3 | 退房流水线中途失败导致部分清理 | 每步独立审计＋可重入（幂等）；失败步骤可单独重跑，不回滚已吊销 token |
| R-A4 | 迁移 0020 与既有 19 个迁移冲突出现多头 | 只新增；`alembic heads` 须单头；merge 须人工读 diff |
| R-A5 | 日历口径压缩（总览 Q1）导致本包超时 | 先裁顺位：A4 的 CSV 筛选维度（FR-ADM-05 属 **P2**）可降级为只按时间筛选；**A2 三道闸门与 A3 重置属 P0，不容顺延** |

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.2 | 2026-11-12 | A3 验收补**退房前端端到端联调判据**：pad 旧 token→401 回登录/未授权态、原绑定家属→403「无权查看该长者的数据」/空态。A 只负责后端 401/403 断言与流水线触发；**前端表现由 D 在 M5.4 联调确认，A 不改 pad/family**。依据：两端既有 401/403 兜底已存在，不需新前端代码。 |
| v1.1 | 2026-11-12 | 全量校验后修正：① 新增 **X12 域清理钩子集**——退房/重置要清的业务表分属 B/C 写域，A **不直写他人表**，改为各域提供 `purge_elder_data`、A 只编排＋审计＋自有三项清理（auth/voiceprint/device）；② A3 由 7h 调为 **6h**（骨架 4h＋联调 2h），包容量 20h→**19h＝2.375 人日**；③ 写域补 `app/services/device_service.py`（退房解绑）；④ 退房流水线明确**每步可重入**、健康脱敏归档改调 C 的钩子；⑤ X4/X12 联调时点统一 D10 16:00，签名 D9 下午冻结。 |
| v1.0 | 2026-11-12 | 首版。承接 Day 9 原生 7 项（三道闸门/日志脱敏/审计导出/教学重置/退房/away/SECURITY.md）＋ day08 遗留 F10 与 F17 的迁移枚举部分；4 节点 20h＝2.5 人日；提供 X8、消费 X4/X9；写域 14 项、禁触清单与 3 处高风险文件先出方案要求明确。实测校准：脱敏分散 4 处无集中模块、`lifecycle_service` 不存在、`audit_log_service` 已存在可复用、console 三入口置灰。 |