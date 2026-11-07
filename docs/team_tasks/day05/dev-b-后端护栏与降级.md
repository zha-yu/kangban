# Task dev-B · 后端 · 护栏与超时降级（guardrails + 三档超时）

> 状态：已完成　｜　版本：v1.1　｜　日期：2026-11-07
> 上游依据：PRD FR-DLG-10（MAX_TOOL_CALLS=8、同 Tool 熔断）｜技术设计文档 §8 Day 5（guardrails.py、三档超时）｜docs/plans/day05 S3–S4
> 联调节点：N2（→dev-C）、N4（→dev-D）、N6（→Day6 Agent）；详见 dag-项目工作流总览与联调节点.md

## 任务清单

| # | 任务 | 前置依赖 | 交付物 | 验收标准 |
| --- | --- | --- | --- | --- |
| B-1 | **guardrails 骨架**：`app/agents/guardrails.py`——`MAX_TOOL_CALLS_PER_TURN=8`、同 Tool 1 分钟滑动窗第 3 次熔断、重复动作检测接口 + `tests/test_guardrails.py` | 无（不依赖 Agent，Day6 注册 Tool 零改动接入） | guardrails.py + 单测 | ① 假工具场景：第 9 次调用被拒并返回如实告知文案 ② 1 分钟内同 Tool 第 3 次触发熔断 ③ 窗口过期计数复位 ④ 常量取值与 AGENTS §7 一致无私改 ⑤ `uv run pytest tests/test_guardrails.py -q` 全绿 ⑥ `check(tool_name) → allow/deny(原因)` 接口签名冻结（N6） |
| B-2 | **服务端三档超时降级**：ASR>2s / LLM>3s / TTS>1s 的并行 watchdog 检测 + try/finally 三件套清理 + per-request state；WS 复用现有 ErrorMsg 通道扩三档 slow code（asr_slow/llm_slow/tts_slow）；话术进常量不进 prompt | 现有音频链路（M3/M4 已通） | 超时降级实现 + ErrorMsg 扩枚举 + 补充测试 | ① 三档逐档触发 slow（11 单测覆盖）② 复用 ErrorMsg 扩枚举（pad 前端零改动，N2 冻结 `code` 字面量）③ 无白屏无静默（并行 watchdog + per-request state 防重复 + 异常不吞、slow 后仍发 unavailable）④ 阈值走 Settings 常量，不散读 env |
| B-3 | **家属端契约技术校对**：对照 TD §3（elder_guardians/daily_reports/notifications）逐端点核对《接口契约_家属端数据》草案 5 端点；发现 2 处偏差后直接修订契约到 v1.1（R3 补 id+assigned_at、S-01 自裁理由纠正），同提交登记修订记录 | 无 | 契约草案 v1.1（已消除偏差）+ 校对评审意见稿交 dev-D | ① 5 端点字段与 TD 表定义逐一对齐 ② 契约文件修订记录同提交，dev-D 在契约冻结评审前暂不按旧草案写代码 |
| B-4 | **Day6 Laya spike 预研**：`spikes/laya_hello/` 安装→加载 checkpoint→跑通→记录 CPU 单句延迟；**已完成**：mock 链路 10/10 通过（~120ms CPU 延迟）、laya 0.4.27 + torch 2.15.0 包装好；**未完成**：真实 laya-serve 因 HuggingFace 421M 权重下载卡在 0%（网络限速） | 确认本地权重获取方式（主理人） | spike 脚本 + README（mock 实测延迟 + 真实模型后续补测） | ① 独立目录**不合并主线** ② 已测 mock 链路延迟为实测非估算；真实模型下载完追加到 README ③ 报告含 4 个 Windows 安装坑位 + TD 1241 中文优化选型说明，供 Day6 6.2 课时直接引用 |

## 联调要求

- **N2**：B-2 事件规范（复用 ErrorMsg 扩三档 slow code：asr_slow/llm_slow/tts_slow）已同步 dev-C，protocol.ts TS Union 已对齐；dev-C 需 grep pad 前端 ErrorMsg 消费模式确认要不要补 switch 分支；联演三档降级录屏。
- **N4**：B-3 契约草案已修订到 v1.1（2 处偏差消除），dev-D 在契约冻结评审前暂不按旧草案写代码。
- **N6**：B-1 `Guardrails.check(tool_name, args=None)` 签名已冻结（app/agents/__init__.py），reset() 调用时机在 guardrails.py 注释里写清（会话生命周期钩子）。
- 完成定义：任务表全绿（B-4 真实模型后续补测标注在 spike README）+ `uv run pytest -q` 682 passed（2 failed 为 pre-existing guardians/voiceprint）+ diff 摘要对照 docs/plans/day05/dev-b-护栏与降级.md v3.1。
