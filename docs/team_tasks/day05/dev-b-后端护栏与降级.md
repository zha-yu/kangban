# Task dev-B · 后端 · 护栏与超时降级（guardrails + 三档超时）

> 状态：未开始　｜　版本：v1.0　｜　日期：2026-11-07
> 上游依据：PRD FR-DLG-10（MAX_TOOL_CALLS=8、同 Tool 熔断）｜技术设计文档 §8 Day 5（guardrails.py、三档超时）｜docs/plans/day05 S3–S4
> 联调节点：N2（→dev-C）、N4（→dev-D）、N6（→Day6 Agent）；详见 dag-项目工作流总览与联调节点.md

## 任务清单

| # | 任务 | 前置依赖 | 交付物 | 验收标准 |
| --- | --- | --- | --- | --- |
| B-1 | **guardrails 骨架**：`app/agents/guardrails.py`——`MAX_TOOL_CALLS_PER_TURN=8`、同 Tool 1 分钟滑动窗第 3 次熔断、重复动作检测接口 + `tests/test_guardrails.py` | 无（不依赖 Agent，Day6 注册 Tool 零改动接入） | guardrails.py + 单测 | ① 假工具场景：第 9 次调用被拒并返回如实告知文案 ② 1 分钟内同 Tool 第 3 次触发熔断 ③ 窗口过期计数复位 ④ 常量取值与 AGENTS §7 一致无私改 ⑤ `uv run pytest tests/test_guardrails.py -q` 全绿 ⑥ `check(tool_name) → allow/deny(原因)` 接口签名冻结（N6） |
| B-2 | **服务端三档超时降级**：ASR>2s / LLM 首 token>3s / TTS 首包>1s 的超时检测与降级动作；WS 下发 `degrade` 事件；话术进常量不进 prompt | 现有音频链路（M3/M4 已通） | 超时降级实现 + 事件常量定义 + 补充测试 | ① 三档逐个人为制造（弄坏 ASR/LLM/TTS）均触发对应降级 ② `degrade` 事件 payload 按规范：`{"stage","message"}`（N2 冻结）③ 无一次白屏或静默（与 dev-C 联演）④ 阈值走 Settings 常量，不散读 env |
| B-3 | **家属端契约技术校对**：对照 TD §3（elder_guardians/daily_reports/notifications）逐端点核对《接口契约_家属端数据》草案 5 端点 | 无 | 校对意见清单（附在契约文件评审稿后） | ① 5 端点（elders 列表/详情、reports 列表/详情、notifications+已读）字段与 TD 表定义逐一对齐 ② 差异只登记不私改，形成 N4 校对稿交 dev-D |
| B-4 | **Day6 Laya spike 预研**：`spikes/laya_hello/` 安装→加载 checkpoint→跑通→记录 CPU 单句延迟 | 确认本地权重获取方式（主理人） | spike 脚本 + 报告（能装/能跑/延迟数字） | ① 独立目录**不合并主线** ② 延迟为实测非估算 ③ 报告含安装坑位记录，供 Day6 6.2 课时直接引用 |

## 联调要求

- **N2**：B-2 事件规范定稿当天同步 dev-C（前端据 `stage`/`message` 渲染），联演三档降级录屏。
- **N4**：B-3 校对稿交 dev-D 与 dev-A（契约草案口径统一，避免两套说法）。
- **N6**：B-1 接口签名冻结后写入 dag 总览 §2.1，Day6 Agent 侧不再改签名。
- 完成定义：任务表全绿 + `uv run pytest -q` 全量绿 + 汇报 diff 摘要（对照 docs/plans/day05 S3–S4 自检清单）。
