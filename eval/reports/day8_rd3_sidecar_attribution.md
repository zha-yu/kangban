# R-D3 归因回执 · sidecar 195ms 预算与评测并发（dev-D → dev-B/主理人）

> 状态：已归因，待裁决处置　｜　版本：v1.0　｜　日期：2026-11-11
> 关联：docs/plans/day08/d-推进计划-日报通知与三端.md（R-D3/Q-D6）｜docs/plans/day07/a3-路由基线.md §6.1/§6.2｜eval/reports/day8_eval_baseline.md
> 上游依据：PRD §5.3 固定意图路由 ≤200ms；a3 附记 195ms 内部预算裁决（2026-11-10）

## 1. 现象

Day 8 在本机复跑 `tests/eval_gate.py --provider sidecar`（sidecar `/health` 正常、
`model_loaded=true`、`laya:multilingual:q2`），四门中 A/B/D 红：普通 21/50、敏感 17/20、
边界 26/30，p50/p95=0ms，整轮 1.4s（110+30 条）。与 S5 首跑基线（41/50、20/20、25/30、19/20）
严重不符。**基线锁未动**（只升不降逻辑保留旧锁），噪声跑产生的报告/backlog 已恢复，未入库。

## 2. 证据（gate 进程日志，逐条可复现）

```
system_one_fast_degraded   reason=timeout          latency_ms=312
system_one_shadow_degraded reason=http_status:503  latency_ms=324
system_one_shadow_degraded reason=http_status:503  latency_ms=300
system_one_fast_degraded   reason=http_status:503  latency_ms=204
system_one_shadow_degraded reason=breaker_open     latency_ms=0   ← 此后全量熔断短路
```

单问探针（`SystemOneClient.decide_fast` 连发 3 次、无 shadow）：188–301ms 正常返回 weather。
即**单问可达、并发必炸**。

## 3. 根因

- sidecar `max_concurrency=1`（a3 §6.1 修复后「许可跟着线程走」），而评测每行发
  **fast＋shadow 两个并发请求**（Router 生产同序，G1 复核不可旁路，a3 §6.2 裁决 shadow 计入）。
- 两请求串行化后各自实测 ≈300ms > 内部硬预算 195ms → 服务端如实 503（overtime）。
- 客户端熔断器在连续 503 后 `breaker_open`，后续行 0ms 短路降级 → 分类器贡献归零，
  裸规则层得分（21/50）冒充模型得分。
- 与 a3 §6.1 三档实测一致：「同时跑 main+MCP+IDE → 稳定 503」；本次即使 main 未起，
  **fast+shadow 自身并发**就足以复现，宿主机负载只是加剧因素。

**归因结论：非 D 侧代码缺陷，非门禁判据缺陷；属 sidecar 性能/评测并发口径问题（A 域＋裁决项）。**
门禁「不回退」判据正确拦截了这次退化，FR-TC-09 机制本身有效。

## 4. 处置建议（待裁决，不擅自改）

1. **评测环境隔离**（推荐，零口径变更）：评测跑在 sidecar 独占核/独立机（a3 §6.1 已建议），
   或评测窗口停 main/IDE 等抢核进程后复跑；复跑绿即解锁 S5/S10 全量门禁。
2. **并发口径裁决**：若认定「评测不应 fast+shadow 并发」，需主理人改 a3 §6.2 口径
   （shadow 旁路化＝G1 变事后统计），属产品口径变更，D 不自行改。
3. **预算不动**：195ms/200ms 为已裁决硬值（PRD §5.3），任何「调大超时」方案违反 a3 纪律，否决。

## 5. 复跑命令

```bash
uv run python tests/eval_gate.py --provider sidecar          # 现状：A/B/D 红（本回执证据）
# 隔离环境后复跑应回绿；锁文件 eval/reports/day8_baseline_lock.json 只升不降
```