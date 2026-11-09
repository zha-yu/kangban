# dev-B · 后端护栏与降级（执行计划）

> 状态：已冻结　｜　版本：v3.2　｜　日期：2026-11-08
> 关联：docs/team_tasks/day05/dev-b-后端护栏与降级.md（任务卡）、docs/plans/day05/day-05-延迟基线与护栏.md（Day5 总计划）
> 上游依据：PRD FR-DLG-10（MAX_TOOL_CALLS=8、同 Tool 熔断）｜技术设计文档 §8 Day 5（guardrails.py、三档超时）｜PRD §5.3（端到端 ≤1.5s 预算）｜AGENTS §7 硬性数值
> 变更记录见文末

---

## 0. 范围与定位

本文件是 Day 5 四人分工中 **dev-B（后端护栏与降级）** 的执行计划，覆盖任务卡 dev-b 的 B-1 ~ B-4 四项。与 Day5 总计划的映射：

| 子任务 | day-05 总计划对应阶段 | 优先级 | 跨人依赖 |
| --- | --- | --- | --- |
| B-1 guardrails 骨架 | S4 · 死循环护栏骨架 | P0 | 无（独立，Day6 Agent 零改动接入） |
| B-2 三档超时降级 | S3 · P50/P95 基线测量（与 dev-A S2/S3 **并行**，方案 B 内部旁路计时，不硬依赖 dev-A） | P0 | N2 联调 ↔ dev-C pad 前端；**不依赖** dev-A A-2 打点点位 |
| B-3 家属端契约技术校对 | S7 · 机动位 A1（穿插执行） | P1 | N4 输出 ↔ dev-D console/family |
| B-4 Laya spike 预研 | S7 · 机动位 A4（穿插执行） | P1 | 本地权重获取方式待主理人确认 |

**不在本计划范围内**（与 dev-B 边界划清）：

| 事项 | 归属 |
| --- | --- |
| metrics 表建表、迁移、打点点位、P50/P95 基线表 | dev-A（A-1/A-2/A-3），本计划 B-2 **完全不依赖** dev-A metrics 表（provider 层内部并行 watchdog 计时，方案 B v3.0） |
| Day6 Laya 三级路由主代码（`app/system_one/` 封装、`app/router/rules.py`） | Day6 主任务，B-4 仅做 spike 预研 |
| 安全一票否决（`app/router/safety.py`） | Day6 L0 规则层 |

---

## 1. 现状盘点（开工前确认，阻塞项先关后门）

| 子任务 | 当前状态 | 已有资产 | 缺失资产 |
| --- | --- | --- | --- |
| B-1 guardrails | `agents/` 目录仅有 `__init__.py`；**骨架未建、单测未写** | `config.py` 常量已就位：`max_tool_calls_per_turn=8`、`tool_repeat_window_seconds=60`、`tool_repeat_break_threshold=3` | guardrails.py 模块、假工具单测 `tests/test_guardrails.py`、`check(tool_name)` 接口签名 |
| B-2 三档超时 | 对话主链（WebRTC 主模式）**无**分段耗时超时检测；按住说话降级链路有模板话术（`app/api/audio.py` 里 `DEGRADE_MESSAGES`）但只覆盖 REST 降级，不覆盖 WS 下发；**方案 B v3 接入点已定位**：① `audio/asr.py` 行 238-272/432-465 Mock+FunASR 两处 adapter 已调 `_observe.start()` 拿 Stopwatch、FINAL/ERROR 时调 `record_asr(first_packet_ms=...)`；② `services/dialogue_service.py` 行 238 `reply = await self._llm.chat(messages)` 是 LLM 非流式整段返回计时点；③ `audio/signaling.py` 行 212-216 已有 `ErrorMsg` TypedDict（`type="error"`, `code` 含四档）+ 行 347-352 `_ERROR_TALKBACK` 话术字典，行 355-366 `deliver()` 接口能给 pad 前端下发任意 ErrorMsg；**两套超时语义已分清**：provider 层（`asr.py` 行 129-130 `FUNASR_RECEIVE_TIMEOUT_S=45.0` / `CONNECT_TIMEOUT_S=5.0`）是连接/空闲兜底，走已有的 `ASRUnavailable → signaling.deliver(code="asr_unavailable")`；B-2 是**真正并行 watchdog**（`asyncio.create_task(asyncio.sleep)` 阈值到了立即发 slow、不等 provider 返回），per-request state（provider 函数内局部 dict）防重复发送，provider 异常**不吞**（slow_degraded=True 后 ASRUnavailable 仍然要发，后端两条都发、前端负责状态升级合并） | ① `ErrorMsg` 已有类型 + 四档话术字典（扩枚举加三档 slow）；② asr.py 两处 adapter 已 import `_observe` 和 `signaling`；③ dialogue_service 已 import `signaling`；④ MockASRProvider 已有 `latency_ms` 参数可精准触发 2s 阈值（单测利器） | ① 扩 `ErrorMsg["code"]` 的 Literal 枚举加三档：`"asr_slow"` / `"llm_slow"` / `"tts_slow"`；② 扩 `_ERROR_TALKBACK` 加三档 slow 话术；③ provider 层**真正并行 watchdog** 骨架（`asyncio.create_task + await sleep`，阈值到了立即发、不等 provider 返回，try/finally 强制清理三件套）；④ dialogue_service 层 LLM 并行 watchdog；⑤ tts.py 层 TTS 并行 watchdog；⑥ per-request state 设计；⑦ 补充测试 + 联演录屏 |
| B-3 家属端契约 | 《接口契约_家属端数据》v1.0 草案已冻结（5 端点 + 11 自裁 + TD 字段逐字抄），dev-B 职责是**技术校对**而非改契约 | 契约草案、TD §3 `elder_guardians/daily_reports/notifications` 三张表定义 | dev-B 产出的校对意见清单（标注「草案口径 vs TD 原文」差异，只登记不私改） |
| B-4 Laya spike | 未创建 | config.py Laya 配置已就位（base_url=7001、timeout_ms=200、confidence_threshold=0.85、fallback_to_llm=True）；TD §8 明确要求先 spike 后合并 | `spikes/laya_hello/` 独立目录、安装脚本、实测延迟数据 |

**阻塞项（开工前须关）：**

| # | 阻塞项 | 来源 | 处置 |
| --- | --- | --- | --- |
| O-1 | B-4 本地 Laya 权重获取方式未确认（TD §8 提「spike 验证后合并」，但未指定权重下载地址） | 主理人 | 开工前向主理人确认权重来源；若确认不了，先搭空壳等权重到位再实测 |

> **v2.0 变更**：原阻塞项 O-2（B-2 分段耗时是否由 dev-A S2 打齐）随方案 B 消掉——B-2 改为 provider 层旁路 watchdog 计时，**完全不依赖 dev-A metrics 表**，ASR/LLM/TTS 三档各自在本层完成超时判定，接线点已定位（见上 B-2 现状盘点行）。

---

## 2. B-1 · guardrails 骨架与单测

### 2.1 任务拆解（最小可执行单元）

| # | 单元 | 文件 | 说明 |
| --- | --- | --- | --- |
| B1-1 | 常量骨架 | `app/agents/guardrails.py`（新建） | 从 `config.py` 读 `max_tool_calls_per_turn` / `tool_repeat_window_seconds` / `tool_repeat_break_threshold`；暴露只读常量供 Day6 注册 Tool 时消费 |
| B1-2 | 核心 API | 同上 | `Guardrails` 类：`__init__(config)`、`check(tool_name, args=None) → Allow / Deny(reason)`、同 Tool 滑动窗计数、重复参数检测、计数器 `reset()`（会话结束调用） |
| B1-3 | 接口签名冻结 | `app/agents/__init__.py`（改） | re-export `Guardrails` 与 `GuardrailDecision`（dataclass：`allow: bool`, `reason: str \| None`）；注释里写「Day6 Tool 注册表零改动接入」，冻结签名 |
| B1-4 | 假工具单测 | `tests/test_guardrails.py`（新建） | 用假工具对象而非真实 MCP 工具：① 第 9 次调用被拒 ② 1 分钟内同 Tool 第 3 次熔断 ③ 窗口过期后计数复位 ④ 相同参数短路返回 ⑤ 常量取值断言（与 AGENTS §7 一致） |

### 2.2 时间分配

| 单元 | 预估耗时 | 优先级 | 依赖 |
| --- | --- | --- | --- |
| B1-1 常量骨架 | 30 min | P0 | `config.py` 常量已就位（无外部依赖） |
| B1-2 核心 API | 90 min | P0 | B1-1 |
| B1-3 接口签名冻结 | 15 min | P0 | B1-2 |
| B1-4 假工具单测 | 60 min | P0 | B1-2 |
| 合计 | **≈ 3 h** | | 无跨人硬依赖，与 S3/B-2 并行执行 |

### 2.3 依赖说明

| 依赖类型 | 内容 | 满足条件 | 未满足时的处置 |
| --- | --- | --- | --- |
| 内部模块 | `app.core.config.get_settings()` | 已满足（配置模块 Day2 交付） | — |
| 外部库 | 标准库 `time` / `collections.deque`；`asyncio`（会话级生命周期管理） | 标准库自带 | — |
| Day6 Agent 接入 | guardrails 本体**不依赖** Tool 注册表；Day6 注册 Tool 时只需 `guardrails.check(tool_name, args)` 前置调用 | 接口签名在本计划 B1-3 冻结 | Day6 改动仅为调用点，guardrails 本体零改动 |

### 2.4 验收标准

| # | 验收项 | 判定方式 |
| --- | --- | --- |
| 1 | 第 9 次工具调用被拒 | 单测：构造 `MaxToolCalls(turn_total=8)`、`guardrails.check(tool="weather")` 第 9 次返回 `Deny(reason="本轮工具调用已达上限")` |
| 2 | 同 Tool 1min 第 3 次熔断 | 单测：同一 tool_name、1 分钟内连续 3 次调用，第 3 次返回 `Deny(reason="工具频繁调用已熔断")` |
| 3 | 窗口过期计数复位 | 单测：`time.sleep(61s)` 后第 4 次调用 `check()` 返回 `Allow`（计时窗口已过期） |
| 4 | 常量取值一致 | 单测断言 `config.max_tool_calls_per_turn == 8`、`tool_repeat_window_seconds == 60`、`tool_repeat_break_threshold == 3`，与 AGENTS §7 / PRD FR-DLG-10 文字吻合 |
| 5 | 单测全绿 | `uv run pytest tests/test_guardrails.py -v` 全 PASS，无 skip、无 warning 升级为 error |
| 6 | 接口签名冻结 | `app/agents/__init__.py` re-export 里明确注释 `check(tool_name, args=None) -> GuardrailDecision`，Day6 Agent 不得再改签名 |

### 2.5 风险预案

| # | 风险 | 概率 | 影响 | 预案 |
| --- | --- | --- | --- | --- |
| R1 | 滑动窗测试计时不准（Windows `time.perf_counter` 在 pytest 内可能精度波动） | 中 | 单测偶现 flaky | ① 用 `unittest.mock.patch` 替换 `time.perf_counter()` 做确定性注入，不依赖真实 sleep；② 真实 sleep 只在超时兜底逻辑里用，不用于窗口测试 |
| R2 | Day6 Agent 注册 Tool 时想新增参数到 `check()`，破坏接口冻结 | 低 | N6 联调卡壳 | `check()` 签名故意做成 **`tool_name: str` + `args: Any \| None`**（args 透传、内部只做 dict 的 hashable 处理），Day6 想加新维度（如 session_id、tool_kind）时由调用方自行拼到 args 字典里，guardrails 不感知 |
| R3 | Agent 重启后计数器状态丢失 | 高（必然发生） | 熔断失效（进程重启就清零） | guardrails 是**进程内内存对象**（不在 Postgres），重启计数清零是设计预期，不做持久化——TD §8 明确「Day5 骨架只处理单进程内滑动窗」，重启恢复归 Day9 audit_logs |

---

## 3. B-2 · 服务端三档超时降级

### 3.1 任务拆解（最小可执行单元 · 方案 B v3：并行 watchdog + per-request state）

> v3 核心变更（相对 v2）：watchdog 从「provider 返回后**事后同步检查** elapsed_ms」改成「**真正并行触发**」——阈值到了立即发 slow 通知、不等 provider；加 `per_request_state` 字典做请求级状态（不是 ConnectionContext 全局属性，天然不跨请求共享）；provider 异常**不吞**（slow_degraded=True 后 ASRUnavailable 仍然要发，前端负责状态升级）。

| # | 单元 | 文件 | 说明 |
| --- | --- | --- | --- |
| B2-1 | 超时阈值常量 + ErrorMsg 枚举扩展 | `app/core/config.py`（改）+ `audio/signaling.py`（改） | ① config 新增三组 Settings：`degrade_asr_slow_ms=2000`、`degrade_llm_slow_ms=3000`、`degrade_tts_slow_ms=1000`；② 扩 `ErrorMsg["code"]` Literal 枚举加三档 slow：`"asr_slow"` / `"llm_slow"` / `"tts_slow"`；③ 扩 `_ERROR_TALKBACK` 话术字典加三档 slow（话术进常量不进 prompt） |
| B2-2 | ASR 并行 watchdog | `app/audio/asr.py`（改） | MockASRProvider + FunASRProvider 两处 adapter 内部，**真正并行 watchdog**（`asyncio.create_task(asyncio.sleep(threshold_s))`，阈值到了**立即**发 slow 通知、**不等** provider 返回）；per-request state 字典 `_state = {"slow_degraded": False}` 记录单次请求是否已发过 slow（provider 函数的**局部变量**，天然 per-request、不跨请求共享）；provider 返回时：① 正常 → 取消 watchdog、清理 `_state`；② 抛异常（含 `ASRUnavailable`）→ **不吞异常**、发 `asr_unavailable`（即使 `slow_degraded=True` 也必须发，前端负责状态升级合并）、清理 `_state` |
| B2-3 | LLM 并行 watchdog | `app/services/dialogue_service.py`（改） | 同 B2-2 模式：`asyncio.wait([chat, watchdog], FIRST_COMPLETED)` 或 `TaskGroup` 统一管理；per-request state 字典；LLM 超时（`self._llm.chat` 抛异常）时**不吞**、发 `llm_timeout`；LLM 当前**非流式**整段返回，Day6 换流式接口时接线点微调（见 §3.5 R12） |
| B2-4 | TTS 并行 watchdog | `app/audio/tts.py`（改） | 同 B2-2 模式：并行 watchdog + per-request state + provider 异常不吞；TTS 首包返回后取消 watchdog |
| B2-5 | 补充测试 | `tests/test_degrade_timeout.py`（新建） | ① 慢响应只发一次（`latency_ms=2500`，断言 WS slow 码**恰好一条**）；② 慢响应后 provider 成功返回（2500ms → slow → FINAL 正常）；③ 慢响应后 provider 失败（2500ms → slow → ASRUnavailable → **仍然发 unavailable**）；④ 下一轮请求 `_state` 天然重置（新函数调用 → 新 dict → slow_degraded=False）；⑤ MockASRProvider `latency_ms=2100` 触发 slow；⑥ monkeypatch LLM 3.2s 触发 llm_slow；⑦ MockTTSProvider `latency_ms=1100` 触发 tts_slow；⑧ watchdog task 清理正常（`asyncio.all_tasks()` 断言主链结束后无残留）；⑨ ErrorMsg 扩枚举 code 取值断言；⑩ 阈值走 Settings、不散读 env |
| B2-6 | 联演与录屏 | 与 dev-C 联调 | 三档逐个演示：slow → 成功、slow → 失败、直接失败三种路径；pad 前端验证 slow → unavailable 状态升级合并（dev-C 需确认 ErrorMsg 消费模式，见 §3.3 新增依赖）；录屏存档 |

#### B2-2 / B2-3 / B2-4 实现伪代码模板（三档通用）

```python
# 在 provider adapter 的 transcribe/chat/synthesize 函数内部
# _state 是函数局部变量，每次调用新建 dict → 天然 per-request
_state: dict[str, Any] = {"slow_degraded": False}
watchdog: asyncio.Task[None] | None = None

async def _slow_watchdog() -> None:
    await asyncio.sleep(settings.degrade_<phase>_slow_ms / 1000)
    # 到达阈值且还没发过 → 发 slow
    if not _state["slow_degraded"]:
        _state["slow_degraded"] = True
        signaling.deliver(conn_id, {
            "type": "error",
            "code": "<phase>_slow",
            "message": _ERROR_TALKBACK["<phase>_slow"],
        })

# 启动并行 watchdog（provider 还没 await，就先启动！）
watchdog = asyncio.create_task(_slow_watchdog())

try:
    result = await real_provider.<call>(...)
except Exception as exc:
    # ★ 不吞任何异常！slow_degraded 即使 True 也必须正确处理异常
    if isinstance(exc, (ASRUnavailable, LLMTimeout, TTSUnavailable, ...)):
        # 发最终失败码（asr_unavailable / llm_timeout / ...）
        signaling.deliver(conn_id, {
            "type": "error",
            "code": "<phase>_unavailable_or_timeout",
            "message": _ERROR_TALKBACK["<phase>_unavailable_or_timeout"],
        })
    raise  # 重新抛出，让上层异常处理链继续工作
finally:
    # ★ try/finally 确保三种路径都清理：正常返回 / 异常退出 / 主任务被取消
    if watchdog and not watchdog.done():
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass
    # _state 随函数帧一起销毁（天然清理，无需显式）

return result
```

#### per-request state 设计说明

| 问题 | 方案 | 理由 |
|---|---|---|
| 标记归属 | **provider 函数内局部 dict** `_state: dict[str, Any] = {"slow_degraded": False}` | 每次函数调用新建 dict → 天然 per-request；不需要改 ConnectionContext schema；Python 不可变类型 bool 在闭包中用 dict 间接修改（dict 是 mutable、闭包可直接赋值） |
| 是否跨请求共享 | **不共享** | 函数调用栈隔离——provider adapter 每次被调用都是新的函数帧、新的局部变量 |
| 并发场景（按住说话降级可能多段音频并发） | 天然安全 | 并发调用 = 多个函数帧 + 多个局部 dict，互不干扰；不需加锁 |
| 显式清理 | 不需要 | 函数返回时 `_state` 随函数帧一起销毁 |

### 3.2 时间分配（方案 B · 消掉 dev-A 依赖后重排）

| 单元 | 预估耗时 | 优先级 | 依赖 |
| --- | --- | --- | --- |
| B2-1 阈值常量 + ErrorMsg 枚举扩展 | 25 min | P0 | 无（config.py + signaling.py 本地改动） |
| B2-2 ASR 旁路 watchdog | 60 min | P0 | B2-1；asr.py 两处 adapter 已 import `signaling` |
| B2-3 LLM 旁路 watchdog | 45 min | P0 | B2-1；dialogue_service.py 行 238 计时点已定位 |
| B2-4 TTS 旁路 watchdog | 45 min | P0 | B2-1；tts.py adapter 模式同 asr.py |
| B2-5 补充测试 | 75 min | P0 | B2-2/3/4；MockASRProvider 已有 `latency_ms` 参数 |
| B2-6 联演与录屏 | 60 min | P0 | B2-5 + dev-C C-1 完成 |
| 合计 | **≈ 4.5 h**（原 v1.0 6.5h，方案 B 省 2h） | | 与 B-1 完全并行；**无 dev-A 硬依赖** |

### 3.3 依赖说明

| 依赖类型 | 内容 | 满足条件 | 未满足时的处置 |
| --- | --- | --- | --- |
| 跨人依赖 | dev-C C-1：pad 前端接收扩枚举后的 ErrorMsg | N2 联调窗口；**开工前需 grep pad 前端代码确认消费模式**（switch/if 分支 vs message 直显） | 若分支覆盖：dev-C 补 slow code 的渲染；若 message 直显：零改动、口头确认即可 |
| 新增（v3） | pad 前端能否正确处理「slow → unavailable」状态升级（两次 error 事件，code 不同） | **需 grep + 联调验证** | 若前端只渲染第一条 error 就丢弃后续，需 dev-C 改逻辑（按 code 优先级 unavailable > slow 覆盖提示）；若前端是 message 直接展示，用户会看到两条提示（可接受、或 dev-C 合并） |
| 内部依赖 | asr.py / tts.py / dialogue_service.py 均已 import `signaling`（不存在循环 import，signaling → asr 是单向，反向 import 在 Python 异步模块里常见） | 已满足（grep 确认三处均已 import） | — |
| 内部依赖 | `app/core/context.py` 的 `trace_id` 透传（ErrorMsg 通过 `deliver()` 注入 connection_id，trace_id 走已有 session 上下文） | 已满足 | — |
| 内部依赖 | MockASRProvider / MockTTSProvider 已有 `latency_ms` 参数（asr.py 行 217） | 已满足 | 单测直接设值，无需新增 mock |
| 新增（v3） | per-request state 设计不需要改 ConnectionContext schema | 已满足（provider 函数内局部 dict 实现） | — |

### 3.4 验收标准（v3 全量重写 · 覆盖 Must Fix 三条）

| # | 验收项 | 判定方式 | 覆盖 |
| --- | --- | --- | --- |
| 1 | 三档 slow 码真正并行触发（阈值到了**立即**发、不等 provider 返回） | MockASRProvider `latency_ms=2500`，`asyncio.wait_for(slow_event, timeout=2.2)` 断言 slow 事件在 2000±200ms 内到达（provider 还在跑、2500ms 才返回）；watchdog 触发时间点用 `time.monotonic()` 打点断言 | Must Fix-1 |
| 2 | slow 只发一次（同请求内） | `latency_ms=2500`，断言 WS slow 码**恰好一条**（watchdog 闭包里 `_state["slow_degraded"]` 置 True 后不再重复发送） | 用户要求 #2 |
| 3 | slow 后 provider 成功 → FINAL 正常结束 | `latency_ms=2500`，WS 收到 slow → provider 返回 FINAL，FINAL 正常走下游流程、不被 watchdog 干扰 | 用户要求 #4 |
| 4 | slow 后 provider 失败 → **仍然**发 unavailable/timeout | `latency_ms=2500` + provider 抛 ASRUnavailable，WS 先收到 slow、**再收到** asr_unavailable（**不吞异常**、两条都发）；前端负责状态升级合并 | 用户要求 #3 + Must Fix-3 |
| 5 | 直接失败（没到 slow 阈值就异常）→ 只发 unavailable | `latency_ms=0` + provider 立即抛 ASRUnavailable，WS 只收到 asr_unavailable、**没有** slow（watchdog 被取消） | Must Fix-3 |
| 6 | 下一轮请求 `_state` 天然重置 | 连续调用 provider 两次，第一次 `latency_ms=2500` 触发 slow、第二次 `latency_ms=2500` 同样触发 slow（新函数帧、新 dict、slow_degraded=False） | 用户要求 #5 |
| 7 | watchdog task 清理完整（无残留） | `asyncio.all_tasks()` 断言主链正常返回后 watchdog 已取消 + await 过；主链异常退出后同样无残留；`finally: cancel()` + `await watchdog` + `except CancelledError: pass` 三件套齐备 | Must Fix-2 |
| 8 | ErrorMsg 扩枚举后 code 取值合法 | `signaling.py` ErrorMsg.code Literal 枚举含 `asr_unavailable` / `llm_timeout` / `tool_breaker` / `internal` + 新增三档 slow；`_ERROR_TALKBACK` 七档话术一一对应 | — |
| 9 | 话术进常量不进 prompt | 三档 slow 话术定义在 `_ERROR_TALKBACK` 字典里，无一处写进 system prompt 或 tool description | — |
| 10 | 无一次白屏或静默 | 三档 watchdog 逐个触发 + provider 成功/失败三种路径，pad 前端正常展示提示（dev-C 联演验证，录屏留证） | — |
| 11 | 阈值走 Settings 常量 | 单测断言 `get_settings().degrade_asr_slow_ms == 2000`、`degrade_llm_slow_ms == 3000`、`degrade_tts_slow_ms == 1000`，不硬编码 | — |
| 12 | ASR / LLM / TTS 三档语义一致 | 用同一个伪代码模板、同一套 per-request state 设计、同一套异常不吞逻辑 | 用户要求 #6 |
| 13 | 测试全绿 | `uv run pytest tests/test_degrade_timeout.py -v` 全 PASS | — |
| 14 | N2 联调签字 | 先 grep pad 前端 ErrorMsg 消费模式 → 确认 slow → unavailable 状态升级能正确展示 → 三档三路径联演录屏 → dev-C 签字 | 新增（v3 补 |

### 3.5 风险预案（v3 重写 · 覆盖 Must Fix 三条）

| # | 风险 | 概率 | 影响 | 预案 |
| --- | --- | --- | --- | --- |
| ~~R4~~ | ~~对话主链分段耗时消费入口难找~~ | **方案 B v3 消掉**（并行 watchdog 接线点已定位） | — | — |
| R5（v3 重写） | watchdog task 清理不完整（内存泄漏 / task 残留 / CancelledError 未 await） | 中 | 会话结束后残留 task 继续跑、误报 slow；`asyncio.all_tasks()` 污染；CancelledError 被吞导致 asyncio 警告 | **强制 try/finally + 三件套**（正常返回 / 异常退出 / 主任务被取消三条路径全覆盖）：`watchdog = asyncio.create_task(...)` → `try: await provider` → `finally: watchdog.cancel() + await watchdog + except CancelledError: pass`。**不使用 TaskGroup**（主任务正常完成时 TaskGroup 不会自动取消仍在等待的 watchdog，可能导致主流程等待阻塞）。单测用 `asyncio.current_task()` + `id()` 断言 watchdog 结束后 `id(watchdog) not in {id(t) for t in asyncio.all_tasks()}` |
| R6（v3 重写） | **双重降级**（slow 先触发 → unavailable 后触发，用户看到两条提示） | 低（后端**不吞异常**是硬要求） | pad 前端渲染两条错误提示 | 后端**仍然两条都发**：slow（阈值到了立即发）+ unavailable（provider 最终失败必须发）。前端负责状态升级合并——pad 前端消费 ErrorMsg 时按 code 优先级 `unavailable > slow` 覆盖提示文案（同一条 UI 位置从"我正在处理"升级到"出了点小状况"）。**不允许**后端用 pass 吞掉 unavailable（已写入验收标准 #4、用户要求 #3） |
| R7 | N2 联调节点 dev-C 没时间处理 slow → unavailable 状态升级 | 中 | 后端完整、前端只渲染一条、用户看不到最终失败 | 联调前 2h 再提醒 dev-C；若 dev-C 排不开，B-2 先跑后端验证（mock WS、断言两条 ErrorMsg 顺序和内容），前端联演并入 S6 演示预演；联演前 grep pad 前端 ErrorMsg 消费模式确认是否需要改 |
| R12 | LLM 当前非流式 `await` 整段返回，并行 watchdog 3s 判定是「整段没回来」而非「首 token 没出」 | 中 | 阈值语义比 PRD 原文「首 token 3s 预算」宽 | Day6 换流式 LLM 接口时，接线点从整段 `await` 改成：**首 token yield 后立即启动 watchdog**（剩余 token 太慢才触发 slow）；整段返回后 watchdog 取消。当前 Day5 阶段非流式整段返回是实际情况，并行判定比不做降级强，语义偏差在 Day6 修正 |
| R13（v3 新增） | per-request state 局部 dict 被并发请求意外共享（理论上不会，但按住说话降级可能多段音频并发） | 极低 | 并发请求的 slow_degraded 标记串味（一个请求发过 slow 导致另一个也不发） | 验证：provider adapter 每次调用都是新函数帧、新局部变量。用 `id(_state)` 断言并发请求的 state dict 地址不同（单测加一条 `asyncio.gather(call1(), call2())` 断言 `id(_state1) != id(_state2)`） |

### 3.6 状态流转图（三档通用）

```
请求开始（函数调用 → _state = {"slow_degraded": False}）
  │
  ├── asyncio.create_task(_slow_watchdog())    ← 立即启动，不等 provider
  │
  ├── await real_provider.<call>(...) 分三种路径：
  │     │
  │     ├── ① provider 在阈值前返回（< 2000ms）
  │     │     ├── 正常返回 → watchdog.cancel() + await → 清理
  │     │     └── 抛异常   → 发 unavailable → watchdog.cancel() + await → 清理
  │     │
  │     ├── ② provider 在阈值后返回（> 2000ms 但最终成功）
  │     │     ├── watchdog 触发 → _state["slow_degraded"]=True → 发 slow
  │     │     ├── provider 返回 → watchdog.cancel() + await → 清理
  │     │     └── 结果正常走下游
  │     │
  │     └── ③ provider 在阈值后失败（> 2000ms 且抛异常）
  │           ├── watchdog 触发 → _state["slow_degraded"]=True → 发 slow  ← 第一条
  │           ├── provider 抛异常 → **不吞**
  │           │     → 发 unavailable                                    ← 第二条
  │           └── watchdog.cancel() + await → 清理 → raise
  │
  └── 函数返回 → _state 随函数帧销毁（天然清理）
       下一轮请求 → 新函数帧 → 新 _state{"slow_degraded": False}
```

**三档一致性**：ASR / LLM / TTS 都走这同一张状态图，只是 phase 名称和具体异常类型不同（ASRUnavailable / LLMTimeout / TTSUnavailable）。

---

## 4. B-3 · 家属端契约技术校对

### 4.1 任务拆解（最小可执行单元）

| # | 单元 | 说明 |
| --- | --- | --- |
| B3-1 | 逐端点核对 TD 字段定义（F0 / F1 / F2 / N1 / N4 × 对应 R1 / R2 / R3 表） | TD §3 的 `elder_guardians`（385-396）/ `daily_reports`（586-598）/ `notifications`（600-617）三张表，逐字段对比契约草案的字段行、类型、可空性、取值枚举 |
| B3-2 | 判定行核对 PRD FR 条款（§4.7 / §4.8 / §2.5） | 契约 §3 的 10 条判定，逐条约去对应 PRD FR/AC 原文验证（如 FR-RPT-02「家属只能查绑定长者」、FR-RPT-03「补录版本」） |
| B3-3 | 自裁清单复核（11 条 S-01~S-11） | 每条自裁标注「TD/PRD 均无 → 裁决 X → 翻案条件 Y」是否写清；字段裁剪（S-05/S-07/S-08）是否与身份契约 S-01 等自裁风格一致 |
| B3-4 | 校对意见清单输出 | `docs/team_tasks/day05/dag-项目工作流总览与联调节点.md` 的 N4 输出；或新建 `docs/plans/day05/dev-b-家属契约校对.md` 作为 dev-D 输入 |

### 4.2 时间分配

| 单元 | 预估耗时 | 优先级 | 依赖 |
| --- | --- | --- | --- |
| B3-1 TD 字段核对 | 60 min | P1 | 《接口契约_家属端数据》v1.0 草案已交付 |
| B3-2 PRD 判定核对 | 45 min | P1 | 同上 |
| B3-3 自裁清单复核 | 30 min | P1 | 同上 |
| B3-4 校对意见清单 | 30 min | P1 | B3-1/2/3 完成 |
| 合计 | **≈ 3 h** | | 机动穿插，不阻塞 S3/S4 主线 |

### 4.3 依赖说明

| 依赖类型 | 内容 | 满足条件 | 未满足时的处置 |
| --- | --- | --- | --- |
| 外部文档 | 《接口契约_家属端数据》v1.0 草案、TD §3 三张表定义、PRD §4.7/§4.8/§2.5 | 已满足（三份文档均在 repo 内） | — |
| 外部角色 | N4 交付给 dev-D 的时间窗（Day5 内） | 与 dev-D 口头约定 | 若 dev-D 优先级不够，校对稿先入计划索引、dev-D 自读 |

### 4.4 验收标准

| # | 验收项 | 判定方式 |
| --- | --- | --- |
| 1 | 5 端点 × 对应 TD 字段逐一对齐 | 校对意见清单里每条差异（含无差异的"完全对齐"）都标注了 TD 原文行号 |
| 2 | 判定行差异只登记不私改 | 契约草案正文**零改动**；所有调整建议集中在 B3-4 校对意见清单里 |
| 3 | 自裁清单 11 条都有翻案条件 | 每条 S-xx 下写清「为什么不按文档」和「翻案条件」；无「暂无翻案条件」的空白行 |
| 4 | N4 签字 | dev-D 确认接收校对稿，dev-B 不越权改动契约正文 |

### 4.5 风险预案

| # | 风险 | 概率 | 影响 | 预案 |
| --- | --- | --- | --- | --- |
| R8 | 校对时忍不住「顺手修正」契约草案里的小错误（字段名拼写、可空性标注） | 高（容易越权） | dev-B 改动契约正文、破坏评审流程 | 严格执行「只登记不私改」原则：发现问题**只写进校对意见清单**、不直接改契约文件。契约冻结权在主理人评审（A1），dev-B 只有技术校对权 |
| R9 | TD 表定义有字段在契约草案里被裁掉（如 notifications 的 `acknowledged_by` 被 S-05 裁掉），dev-D 联调时需要这些字段 | 中 | dev-D Mock 对接时缺字段 | 在校对稿里专门开一节「裁剪字段说明」，标注「契约裁掉字段列表 + 裁掉理由 + 联调影响」，让 dev-D 清楚 Mock 数据里哪些字段不出 |

---

## 5. B-4 · Day6 Laya spike 预研

### 5.1 任务拆解（最小可执行单元）

| # | 单元 | 说明 |
| --- | --- | --- |
| B4-1 | 目录搭建 | `spikes/laya_hello/`（新建）：`spike_probe.py`（独立脚本，不 import `app/` 任何模块）、`README.md`（能装→能跑→记延迟的操作手册） |
| B4-2 | 权重加载验证 | 按主理人确认的权重来源安装依赖、加载 checkpoint、跑官方 demo（一条前向推理） |
| B4-3 | CPU 单句延迟实测 | 用 Python `time.perf_counter()` 跑 ≥10 次前向、取 P50/P95；记录「从输入文本到输出四信号」的 wall-clock 时间 |
| B4-4 | 安装坑位记录 | 记录所有 pip install 报错、onnxruntime 版本坑、CPU 推理强制关 CUDA、Python 版本兼容等；供 Day6 6.2 课时直接引用 |

### 5.2 时间分配

| 单元 | 预估耗时 | 优先级 | 依赖 |
| --- | --- | --- | --- |
| B4-1 目录搭建 | 20 min | P1 | 独立，不动主线 |
| B4-2 权重加载验证 | 90 min | P1 | O-1 阻塞项解决（本地权重来源确认） |
| B4-3 延迟实测 | 30 min | P1 | B4-2 |
| B4-4 坑位记录 | 30 min | P1 | B4-2/3 |
| 合计 | **≈ 3.5 h** | | 机动穿插；**不合并主线**；若权重迟迟不到，先搭空壳 |

### 5.3 依赖说明

| 依赖类型 | 内容 | 满足条件 | 未满足时的处置 |
| --- | --- | --- | --- |
| 外部阻塞 | 本地 Laya 开源权重获取方式（TD 未指定下载地址） | 待主理人确认 | 开工前先问主理人；若一周内确认不了，B-4 降为「空壳 spike 骨架 + 权重到位说明」，Day6 前再补实测 |
| 内部资源 | 无（spike 独立，不 import `app/`） | — | — |

### 5.4 验收标准

| # | 验收项 | 判定方式 |
| --- | --- | --- |
| 1 | 独立目录不合并主线 | `spikes/laya_hello/` 不被 import 进 `app/`；spike 脚本只 import 第三方 Laya 包 |
| 2 | 延迟数字实测非估算 | README 里明确标注「实测 P50/P95」，不是「估算 150ms」 |
| 3 | 安装坑位齐全 | README 里列所有踩过的坑 + 解决方案，格式参考 `spikes/reference/asr/README.md` |

### 5.5 风险预案

| # | 风险 | 概率 | 影响 | 预案 |
| --- | --- | --- | --- | --- |
| R10 | 本地 Laya 权重迟迟不到 | 高 | Day6 前无法完成 spike 实测 | 先搭 spike 空壳骨架 + README 占位 + 权重下载说明，Day6 6.2 前权重到位再补实测；最坏情况 Day6 先按 config.py 已有的 `laya_timeout_ms=200`（与 PRD §7 快决策阈值 0.85 对齐）硬编码跑，spike 延迟复测并入 Day7 |
| R11 | Laya 开源依赖在 Windows 上装不动（onnxruntime-gpu / torch / CUDA 版冲突） | 中 | spike 卡死 | 切换到 CPU-only 推理（TD §1.1 明确 Laya CPU 推理即可）；onnxruntime 改用 `onnxruntime==1.17.0` 稳定版；若仍卡，在 WSL 里跑（经验：Windows Python 科学计算包兼容性差于 WSL） |

---

## 6. 联调节点核对清单（逐项，对接即验收）

| # | 对接双方 | 对接物 | 接口规范 | 完成判定 | 计划里的位置 |
| --- | --- | --- | --- | --- | --- |
| **N2** | dev-B ↔ dev-C | 三档 slow 降级事件 | WS 下发复用 ErrorMsg：`{"type":"error","code":"asr_slow\|llm_slow\|tts_slow","message":<常量话术>}`；pad 已消费 error 事件、扩枚举后**零改动**，dev-C 只需确认渲染逻辑覆盖新增三档 code | B-2 联演三档逐个触发，前端展示提示文字、不白屏；联演录屏存档 | B-2 §2.2 时间分配；§3.4 验收 1-9 |
| **N4** | dev-B ↔ dev-D | 家属端契约校对稿 | 对照 5 端点逐字段核对；差异只登记不私改；标注「草案口径 vs TD 原文」 | dev-D 接收校对稿，dev-B 不改契约正文 | B-3 §3.2 时间分配；§4.4 验收 1-4 |
| **N6** | dev-B ↔ Day6 Agent | guardrails 预留接口 | `check(tool_name, args=None) -> GuardrailDecision(allow: bool, reason: str\|None)` | Day6 注册 Tool 零改动接入 guardrails；签名冻结写入 `agents/__init__.py` | B-1 §2.1 B1-3；§2.4 验收 6 |

---

## 7. 执行顺序总览

```
Day5 总计划 S0（前置收口）先完成 →
  并行主线（无任何跨人硬依赖可先行）：
    B-1 guardrails（≈ 3h，完全独立，无阻塞）
    B-2 三档超时（≈ 4.5h，方案 B provider 层旁路 watchdog；**不依赖 dev-A A-2**；接线点已定位，可立即开工）
  机动穿插（主线并行时抽空做）：
    B-3 家属契约校对（≈ 3h，输出 N4 校对稿）
    B-4 Laya spike（≈ 3.5h，等权重来源后触发）
  全部收束 → 写入 N2/N4/N6 联调证据 → Day5 总计划 M5.2（基线表）+ M5.3（护栏用例全绿）里程碑
```

---

## 8. 自检清单（每阶段完成后复跑）

- [ ] B-1：`uv run pytest tests/test_guardrails.py -v` 全绿，无 skip、无 flaky
- [ ] B-1：`uv run ruff check app/agents tests/test_guardrails.py` 无 error
- [ ] B-1：常量取值与 AGENTS §7 / PRD FR-DLG-10 文字完全一致，无私改
- [ ] B-2：三档阈值语义分清——**真正并行 watchdog**（`asyncio.create_task` + `asyncio.sleep`，阈值到了立即发 slow、不等 provider 返回）；per-request state 防重复发送；provider 异常**不吞**（slow_degraded=True 后 ASRUnavailable 仍然发，后端两条都发、前端负责状态升级）；provider 断网兜底走 `"asr_unavailable"` 码，两套超时走不同异常路径、不会双重触发
- [ ] B-2：扩 ErrorMsg 枚举 + `_ERROR_TALKBACK` 话术字典，话术进常量不进 prompt，WS 复用已有 error 通道（不新建 degrade 事件）
- [ ] B-2：`app/api/audio.py` 的 `DEGRADE_MESSAGES`（REST 降级）与 `_ERROR_TALKBACK`（WS slow 码）去重，两套话术合并为一处真源（或显式标注差异原因）
- [ ] B-2：watchdog 是 `asyncio.create_task` 旁路，**try/finally 强制清理三件套**（正常返回 / 异常退出 / 主任务被取消三条路径全覆盖：`watchdog.cancel() + await watchdog + except CancelledError: pass`）；**不使用 TaskGroup**；`asyncio.all_tasks()` 断言主链结束后 watchdog 无残留
- [ ] B-2：**无 dev-A metrics 表依赖**——asr.py / dialogue_service.py / tts.py 各自本层完成判定，不 import `services/metrics_service` 或 `models.metrics`
- [ ] B-3：《接口契约_家属端数据》正文零改动，校对意见独立文件
- [ ] B-4：`spikes/laya_hello/` 不 import `app/`，不合并主线；延迟数字实测非估算
- [ ] 全量：`uv run pytest -q` 无新增失败；`uv run ruff check app tests` 全绿
- [ ] 全量：`git diff --stat` 对照 dev-b 任务卡的交付物清单，无越界改动

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-07 | 首版：基于 dev-b 任务卡、Day5 总计划、TD §8 Day5、PRD FR-DLG-10、docs/接口契约_家属端数据 v1.0 草案，拆解 B-1 ~ B-4 四项；每项含拆解 / 时间 / 依赖 / 验收 / 风险；联调节点 N2/N4/N6 核对清单；8 条自检项 |
| v2.0 | 2026-11-07 | **方案 B 全量回改**：① B-2 从「消费 dev-A metrics 表轮询」改成「provider 层旁路 watchdog + 扩 ErrorMsg 枚举」——读代码发现 `asr.py` 两处 adapter 已拿 Stopwatch、`dialogue_service.py` 行 238 LLM 计时点已定位、`signaling.py` 已有四档 ErrorMsg 话术字典可直接扩枚举；② 消掉阻塞项 O-2（B-2 完全不依赖 dev-A A-2）；③ 时间分配 B-2 从 6.5h 压到 4.5h（省 2h）；④ R4 风险消掉、新增 R12（LLM 非流式整段返回语义偏差 Day6 修正）；⑤ N2 联调节点从「新建 degrade 事件」改成「复用 error 通道、pad 前端零改动」；⑥ 现状盘点补代码行号和 grep 证据；⑦ 状态从「草稿」改「进行中」 |
| v3.0 | 2026-11-07 | **第二轮技术审查 · Must Fix 三条全量修复**：① watchdog 模式从「provider 返回后事后同步检查」改成「真正并行触发」（`asyncio.create_task(asyncio.sleep)`，阈值到了立即发 slow、不等 provider 返回）；② 加 per-request state（provider 函数内局部 dict，天然 per-request、不跨请求共享）；③ provider 异常**不吞**（slow_degraded=True 后 ASRUnavailable 仍然要发，后端两条都发、前端负责状态升级合并）；④ watchdog task 清理强制三件套（cancel + await + CancelledError pass）；⑤ 伪代码模板补齐（三档通用）；⑥ 新增 §3.6 状态流转图；⑦ 验收标准重写为 14 条（覆盖 Must Fix 三条 + 用户 6 条要求 + 三档一致性）；⑧ 依赖说明补 pad 前端慢→不可用状态升级验证；⑨ 风险预案 R5/R6 重写、新增 R13（并发串味）；⑩ B2-5 测试用例从 7 条扩展到 10 条（新增慢响应只发一次、慢后成功、慢后失败、下一轮重置四条） |
| v3.1 | 2026-11-07 | **编码前收尾**：① watchdog 清理统一用 try/finally（伪代码模板和 R5 全量改写，正常返回 / 异常退出 / 主任务被取消三条路径全覆盖）；② 明确不使用 TaskGroup（主任务正常完成时 TaskGroup 不会自动取消仍在等待的 watchdog，可能阻塞主流程）；③ 清理三处 v2.0 旧方案残留：§0 不在范围表「B-2 消费 dev-A metrics」→「完全不依赖 dev-A」、§1 现状盘点 B-2 行「Stopwatch elapsed_ms 事后检查」→「真正并行 watchdog」、§8 自检清单「只在 provider 成功返回后检查 elapsed_ms」→「并行 watchdog + per-request state + 异常不吞」；④ §8 自检清单新增 try/finally 三件套检查项 |
| v3.2 | 2026-11-08 | 状态收口：任务卡已置「已完成」，本计划状态由「进行中」改「已冻结」，并校正头部版本 v2.0 → v3.2（v3.0/v3.1 变更此前仅记于文末）。B-1/B-2/B-3 已交付，B-4 真实模型补测仍标注在 spike README |
