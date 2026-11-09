# Task dev-A · 后端 · 观测与数据链路（metrics 全链路）

> 状态：进行中（A-1/A-2/A-4 已交付，A-3 待 N1）　｜　版本：v1.1　｜　日期：2026-11-08
> 上游依据：技术设计文档 §8「Day 5 · 延迟与护栏」｜PRD §5.3（AC-40/42）｜docs/plans/day05/day-05-延迟基线与护栏.md S1–S3
> 联调节点：N1（→dev-C）、N3（→dev-D）；详见 dag-项目工作流总览与联调节点.md
> v1.1 变更：A-1 metrics 三件套（0006 迁移 + ORM + metrics_service）与 A-2 打点收口已落地；A-4 契约草案 v0.1 已交付（6ad892e）；A-3 待 dev-C N1 测量数据后出基线表。

## 任务清单

| # | 任务 | 前置依赖 | 交付物 | 验收标准 |
| --- | --- | --- | --- | --- |
| A-1 | **metrics 表落地**：ORM 模型 + 新迁移 `0006_day5_metrics.py` + `app/services/metrics_service.py`（唯一写库层） | 无（文档留痕三件套已由计划侧完成，PRD v2.8 同批先例） | 模型/迁移/service 三件；ER 与 TD 修订记录同步条目（伴随交付，同一次提交） | ① `alembic upgrade head` 成功且不触碰 0001–0005 ② 表结构三处（ER/TD/ORM）一致 ③ 字段：trace_id、elder_id 可空、stage 枚举七值、duration_ms、source、created_at；索引 (trace_id)、(stage, created_at) ④ 全量 type hint、I/O async |
| A-2 | **打点收口进库**：只改 `app/core/metrics.py` 与 `app/audio/_observe.py` 单点收口函数体，日志照旧 + 异步批量入库 | A-1 | 两文件最小 diff + `metrics_service.record_stage(...)` 补充测试 | ① 六段打点齐：wakeup/connection/asr/route/llm_first_token/tts_first_packet/playback；端侧 wake_ts 优先、WS accept 兜底 ② `api/audio/scheduler/graphs` 未直接写库 ③ 抽 3 个 trace_id 双轨一致 ④ 无一行含转写全文/隐私（`test_audio_*`、`test_core_metrics.py` 全绿） |
| A-3 | **P50/P95 复算 SQL + 基线表生成** | A-2、dev-C 测量完成（N1） | `docs/plans/day05/day-05-延迟基线表.md` + SQL 语句 | ① 数字由 SQL 对 metrics 表复算、报告附查询可复现 ② ASR 段按「本地 FunASR 基线」单独归档并注明 O-1（首包 2219ms）与云 key 复测条件 ③ 20 次样本量、环境说明齐 ④ ≤1.5s 达标次数如实记录不美化 |
| A-4 | **console 五 Mock 页接口契约草案** | 无（可与其他任务并行） | `docs/接口契约_照护台业务数据.md`（标注「待冻结」） | ① 覆盖工单/告警通知/健康代填/药单/复核五组端点 ② 每个字段行标注 TD §3 表名+字段出处，无凭空字段 ③ 与前端现状 Mock 字段差异清单（供 dev-D D-3 对齐） |

## 联调要求

- **N1**：A-2 完成后立刻通知 dev-C 开测（打点口径即 N1 规范：`record_stage(trace_id, elder_id|None, stage, duration_ms, source)`，不含隐私字段）。
- **N3**：A-4 草案当天交 dev-D 对齐，差异双向确认后登记，不私改。
- 完成定义：任务表全绿 + `uv run pytest -q` 全量绿 + 汇报 diff 摘要（对照 docs/plans/day05 S1–S3 自检清单）。
