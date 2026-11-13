> 状态：可执行（待 M6.0 启动核对后开工）　｜　版本：v1.0　｜　日期：2026-11-13
> 负责人：**dev-B（唯一）**　｜　任务包：**B · 观测与成本线**　｜　容量：上限 24h（3 人日）；实排开发 **9.5h ＝ 1.19 人日**，余额为 Line 1 联调槽位占用＋缓冲（`app/main.py` 装配与 `app/scheduler/` 既有写锁仍归你，Line 1 收口优先）
> 关联：docs/team_tasks/day10/day10-任务总览与DAG.md v1.0（§2 DAG、§3 对接点 Y2/Y5、§4 写域）｜docs/plans/day10/day-10-交付与观测-工作推进计划.md v1.0 §4.2
> 上游依据：PRD line 513（AC-31 完整 Trace）、line 453–460（FR-TC-04 脱敏上传/FR-TC-08 成本指标）、line 587（F23 P2 顺延）｜技术设计文档 §8.3 Day 10（Langfuse 最小实现）
> 变更记录见文末

---

# dev-B · Day 10 任务书 · 观测与成本线

## 0. 你的边界（先读这条）

### 0.1 写域仅限（越界即返工）

```
Langfuse 接入代码（观测层模块；文件落点在 M6.0 定，建议 app/core/observability.py 或既有 app/audio/_observe.py 收编）
app/core/config.py                    （仅 langfuse 段增量）
app/system_one/**                     （B3）
privacy_scope 消费点（B3）
app/api/metrics.py                    （增量）
.env.example                          （langfuse 变量段；先定稿交 A 合编排，B→A 顺序）
eval/reports/day10_langfuse_trace.md  （新增）
```

### 0.2 禁触（他人写域，需改动时提需求不直接改）

`Dockerfile`/`docker-compose.yml`/`deploy/nginx.conf`（dev-A 独占）、`app/scheduler/device_jobs.py`（A5 新文件，不碰）、前端构建（C）、`tests/eval_gate.py`/`.githooks/`（D）、`README.md`（C 独占——你的成本估算以「章节素材小抄」交 C 合入）、docs 交付文档三件（C）。

### 0.3 高风险文件（AGENTS §5）

Langfuse 接入涉及运行时装配：接入模块**先出方案**（写在本文件 §6 执行记录）经确认后落代码；确保 `langfuse_enabled=False` 时业务零影响（降级不白屏）。

### 0.4 角色口径

本包**不含**独立测试/产品/设计角色工作。观测接入的自验证（Trace 证据落档）是你作为该域开发的内嵌交付物。

### 0.5 你的 Line 1 槽位（优先级高于本包）

D10 穿插 X11 槽（15:00）。冲突时 Line 1 优先。

---

## 1. 节点 B1 · Langfuse 接入（FR-TC-04 / AC-31，5h）

**前置**：M6.0；联调依赖 **Y2**（A2 的 langfuse 服务与凭据，D11 上午 compose 就绪后）。
**对接**：产出 M6.2 判据；证据供 C2 观测章节（素材）；B2 依赖本节点。

### 要做什么

1. 启用 `langfuse_enabled`（`.env` 注入 key，Y2 契约；`app/core/config.py:212-216` 已有 5 个 `langfuse_*` 字段，只增量不改名）。
2. 每次主控 Agent 执行 ＝ 1 个 Trace；规则路由 / 分类器 / 模型调用 / 工具调用 / TTS 各为 1 个 Span。
3. Span 记录：输入输出摘要、耗时、异常、模型版本。
4. **脱敏后上传**：复用 `app/core/redact.py` 集中脱敏（day09 A1 交付）；不含完整音频、密钥、不必要健康信息。
5. 采样率沿用 `langfuse_sample_rate` 配置。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | Langfuse 接入代码（观测层增量） | 发起一次含工具调用的完整对话 → Langfuse 界面可见 1 条完整 Trace 且 Span 链＝规则→分类器→模型→工具→TTS |
| 2 | 脱敏证明 | Trace 内 grep 手机号/身份证/密钥零命中 |
| 3 | 降级证明 | 关闭 `langfuse_enabled` 后业务不中断（降级不白屏） |
| 4 | `eval/reports/day10_langfuse_trace.md`（AC-31 证据） | 含 trace_id 可复现 |

---

## 2. 节点 B2 · /metrics 成本口径补齐（FR-TC-08，2h）

**前置**：B1。
**对接**：产出素材给 C2（README 成本章节）。

### 要做什么

1. 核对既有分层命中率/S1 闭环率/token 消耗（day09 C4 已交付 `app/api/metrics.py`）。
2. 补 **prompt cache 命中率**统计口径（大纲 10.2 与 PRD FR-TC-08 均要求；现状未见字段）。
3. 一次完整演示后导出 token 消耗与成本估算。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | `app/api/metrics.py` 增量 | `curl --noproxy '*' -H "Authorization: ..." http://127.0.0.1:8000/metrics` 可见四类指标（分层命中率/S1 闭环率/token/cache）；未授权访问 401 |
| 2 | 成本估算记录 | cache 命中率数字可复算（来源对话数、命中数可查）；素材交 C 合入 README |

---

## 3. 节点 B3 · F23 四信号 ＋ privacy_scope 消费方（2.5h，day09 顺延项 P2，D12）

**前置**：M6.0。**对接**：无硬下游，D2 回归覆盖。

### 要做什么

1. 快路径补齐 System One 四信号输出：intent / is_sensitive / confidence / privacy（敏感度、升级建议、隐私收敛）。
2. `privacy_scope` 接入至少 1 个消费方（双人间收敛场景）。
3. **延迟口径**：day09 实测四问 p95 668ms > 200ms 硬线，本节点按「信号先齐、延迟如实记录」执行；不为达标调阈值（沿 X9 纪律），达标改造另立任务【总览 §8 Q8】。

### 交付物与自检校验标准

| # | 交付物 | 自检判据 |
| --- | --- | --- |
| 1 | `app/system_one/` 增量 | 四信号在快路径输出齐全（intent/is_sensitive/confidence/privacy） |
| 2 | `privacy_scope` 消费接线 | 至少 1 个消费点可演示 |
| 3 | 延迟记录 | 复跑 50 条样例记录 p95；如仍 >200ms **如实记录不粉饰**并登记总览 §8 Q8 |

---

## 4. 执行记录（开工后填写）

| 日期 | 节点 | 结果/证据指针 | 等待项 |
| --- | --- | --- | --- |
| | | | |

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-13 | 首版，与总览 v1.0 同批产出。 |
