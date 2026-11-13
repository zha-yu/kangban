# Day 10 B2 · 成本素材小抄（交 dev-C 合入 README 成本章）

> **交付**：dev-B → dev-C（2026-11-13）。dev-C 合入 README 时**填价格、算成本**，本小抄只提供实测数字与口径，不代填单价（价格随厂商变动，以当日定价页为准）。
> **数据来源**：`/api/metrics`（进程内计数）＋ Langfuse v4 实例 ClickHouse span 库（跨重启留痕）＋ 联调对话日志。全部实测，无估算值；缺口如实标注。

## 1. 路由分层（省 Token 的结构性来源）

实测时点 2026-11-13 19:11（`GET /api/metrics`，含全天联调流量）：

| 层 | 调用数 | 占比 | 成本含义 |
| --- | --- | --- | --- |
| fallback | 112 | 72.73% | 走 LLM，最贵（注：联调期含大量 429 降级与异常探针，生产占比待 Day 11+ 观测） |
| rule | 24 | 15.58% | 零 LLM 成本（工具短路直调） |
| system_one | 18 | 11.69% | 零 LLM 成本（快路径） |
| S1 闭环率 | — | 5.19% | 快路径闭环比例 |

**口径提醒（C2 合入时务必写明）**：分层占比是**省 token 的结构上限**——rule/system_one 层每接住一通对话即省一次完整 LLM 往返；fallback 占比越高说明越依赖 LLM。

## 2. Token 消耗（实测）

**MiniMax 档（`MiniMax-M2.7-highspeed`，当日配额耗尽前）**：

| 轮次（trace） | prompt | completion | 说明 |
| --- | --- | --- | --- |
| `456639a7…` 第 1 次 LLM 调用 | 765 | 41 | 无城市问句 → LLM 反问城市 |
| `456639a7…` 第 2 次 LLM 调用（工具轮后续调用） | 765+ | 58 | agent 循环内第二次调用 |
| 其他成功轮 | 765 量级 | 40~60 量级 | 单轮 prompt 基线 ≈765 tokens（系统提示+人设+历史） |

**Qwen 档（`qwen3.8-flash`，19:30 起切换后）**：

| 指标 | 值 | 复算口径 |
| --- | --- | --- |
| LLM 调用数 | 3 | 进程内计数（19:30 重启后归零起算） |
| prompt_tokens 合计 | 2025 | ≈675/次，与 MiniMax 基线同量级 |
| completion_tokens 合计 | 531 | **含 reasoning_tokens**（qwen3.8 是思考模型，thinking 也计费——C2 定成本时注意拆分） |
| 单轮端到端 | 见 Langfuse trace `82d32606…` | llm_first_token span 有真实耗时 |

## 3. Prompt Cache 命中率（FR-TC-08）

- **口径**（§8.1-⑦ 裁决）：进程内计数 + LLM span 属性明细，`/api/metrics` 的 `prompt_cache` 段；重启归零并在响应中如实标注 `available/reason`。
- **Qwen 实测**（3 次调用，均为冷缓存）：`cache_hit_tokens=0`，`cache_miss_tokens=2025`，`hit_rate=0.0`——可手工复算：0/(0+2025)=0.0。**命中 0 是真实值不是缺数据**：Qwen 兼容端点要求重复相同前缀且达到最小长度才触发 implicit cache，联调对话前缀差异大未触发。
- **观察口径**：多轮会话共享稳定系统提示 + 人设时才可能产生命中；演示建议用同一长者连聊多轮再读数。
- **MiniMax 口径**：当日版本未回传 cache 字段（`calls_with_cache_info` 不计），故 cache 数据仅 Qwen 档有。
- **已知缺陷（待 dev-A）**：`redact.py` `_SECRET_KEY_RE` 误伤 `*_tokens` 字段名，日志轨与 Langfuse 属性里的 token 数暂显示 `***`，修复合入后明细可查（`/api/metrics` 快照数字不受影响，本小抄数字来自快照与原始 API 响应）。

## 4. 成本公式（C2 填价格）

```
单轮 LLM 成本 ≈ (prompt_tokens × 输入单价 + completion_tokens × 输出单价)
分段成本 ≈ Σ(各层调用数 × 该层单轮成本)     # rule/system_one 层 = 0
缓存节省 ≈ cache_hit_tokens × (输入单价 − 缓存命中单价)
```

价格来源建议：百炼定价页 `qwen3.8-flash` 当日价（注意 reasoning tokens 计费方式）；MiniMax 档留存备查。

## 5. 复现路径

1. 起后端：`uv run uvicorn app.main:app --port 8000`
2. 设备换会话：`POST /api/device-sessions`（device_id 见 seed）
3. 跑对话：`POST /api/audio/utterance`（WAV + duration_ms；无城市问句走 agent 路径产生 LLM 调用）
4. 读数：`GET /api/metrics`（Bearer admin token；admin 账号 `13700000001`）
5. Langfuse 留痕：http://localhost:3000 → trace_id 即响应头/日志中的系统 trace_id
