"""A2 延迟/降级复测脚本（Day 7 · dev-A）：给「换一台干净机器复测」用。

为什么单独一个脚本，而不是继续手敲 curl：

- A2 的两条验收（p95 ≤200ms、停 sidecar ≤250ms 降级）都是**数值判定**，必须可重复、可留证；
- 判定必须由脚本给出 PASS/FAIL，不能靠人看 10 行 curl 猜；
- 机器状态（核数/负载/是否有别的模型常驻）要写进结果——上一轮唯一没拿到的结论就是
  「这台机 IDE 吃满 99% CPU 时前向退化到秒级」，不记录环境就没法解释数字。

三个模式（默认全跑）：

```bash
# 0. 先起 sidecar（必须用 jev 环境，主 .venv 无 torch）
#    PYTHONPATH=<repo> D:/Users/.../envs/jev/python.exe -m uvicorn app.system_one.server:app --port 7001
uv run python scripts/sidecar_bench.py                    # http + degrade
uv run python scripts/sidecar_bench.py --mode http --requests 40
uv run python scripts/sidecar_bench.py --mode degrade     # 不起 sidecar 也能跑（就是验降级）
uv run python scripts/sidecar_bench.py --mode pure        # 纯前向基线，必须用 jev 环境跑
```

判定口径（对齐计划 §5 总表）：

| 检查 | 通过条件 |
| --- | --- |
| 健康 | `/health` 200 且 `model_loaded=true` |
| 快路径 p95 | 成功请求的 **客户端** p95 ≤200ms（PRD §5.3） |
| 成功率 | 200 占比 ≥90%（低于此值说明 195ms 内部预算被挤爆，需看环境表） |
| 降级 | sidecar 不在时 ≤250ms 返回，且 `degraded=True` + 三不变量成立 |

`--mode pure` 只量模型本身（不含 HTTP），用来判断「慢是模型的锅还是环境的锅」。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.core.config import Settings
from app.system_one.client import SystemOneClient
from app.system_one.questions import build_fast_questions

#: 复测用的固定句子（不与评测集耦合：这里只量延迟，不量准确率）
PROBE_TEXTS: tuple[str, ...] = (
    "北京今天冷不冷",
    "放段戏听听吧",
    "我吃过药了，早上那顿吃了",
    "我今天心里挺高兴的，孙子来看我了",
    "帮我记一下血压，高压一百三低压八十",
)

#: 判定阈值（与计划 §5 一致，改这里等于改验收）
FAST_P95_BUDGET_MS: int = 200
SUCCESS_RATE_FLOOR: float = 0.90
DEGRADE_BUDGET_MS: int = 250


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def _env_report(settings: Settings) -> dict[str, Any]:
    """把环境写进结果：上一轮「IDE 吃满 99% CPU」就是靠这个才说得清。"""
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cpu_logical": os.cpu_count(),
        "base_url": settings.system_one_base_url,
        "client_timeout_ms": settings.system_one_timeout_ms,
        "shadow_timeout_ms": settings.system_one_shadow_timeout_ms,
        "model_version": settings.system_one_model_version,
        "provider": settings.system_one_provider,
    }


async def _health(base_url: str) -> tuple[bool, dict[str, Any]]:
    try:
        # trust_env=False：见 app/system_one/client.py 的注释——开发机的 HTTP_PROXY
        # 会把 127.0.0.1 也绕进代理，健康检查会误判「sidecar 不可用」
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(f"{base_url}/health")
            body = response.json()
    except Exception as exc:  # 没起就是 False，不抛
        return False, {"error": f"{type(exc).__name__}: {exc}"}
    ok = response.status_code == 200 and bool(body.get("model_loaded"))
    return ok, body


async def run_http(settings: Settings, requests: int) -> dict[str, Any]:
    """HTTP 模式：模拟 Router 的快路径调用（只问 intent、带 state、200ms 预算）。"""
    ok, body = await _health(settings.system_one_base_url)
    if not ok:
        return {"mode": "http", "error": "sidecar 不可用或模型未加载", "health": body}

    client = SystemOneClient(settings)
    latencies: list[float] = []
    failures: list[str] = []
    try:
        for index in range(requests):
            text = PROBE_TEXTS[index % len(PROBE_TEXTS)]
            started = time.perf_counter()
            result = await client.decide_fast(
                text,
                trace_id=f"bench-{index:03d}",
                state={
                    "asr_final": text,
                    "current_task": "none",
                    "media_status": "idle",
                },
            )
            elapsed = (time.perf_counter() - started) * 1000
            if result.degraded:
                failures.append(f"degraded@{elapsed:.0f}ms:{text}")
            else:
                latencies.append(elapsed)
    finally:
        await client.aclose()

    success_rate = len(latencies) / requests if requests else 0.0
    return {
        "mode": "http",
        "requests": requests,
        "health": body,
        "success": len(latencies),
        "degraded": len(failures),
        "success_rate": round(success_rate, 3),
        "p50_ms": round(_pct(latencies, 0.50), 1),
        "p95_ms": round(_pct(latencies, 0.95), 1),
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
        "degraded_samples": failures[:5],
    }


async def run_degrade(settings: Settings) -> dict[str, Any]:
    """降级模式：指向一个必然连不通的端口，验「不抛异常 + ≤250ms + 三不变量」。"""
    dead = settings.model_copy(update={"system_one_base_url": "http://127.0.0.1:1"})
    client = SystemOneClient(dead)
    try:
        # 冷启动第一次会含连接池初始化；同时记录冷/热两个数（口径见报告 §6.1）
        cold_start = time.perf_counter()
        cold = await client.decide_fast("测试一句", trace_id="bench-cold")
        cold_ms = (time.perf_counter() - cold_start) * 1000

        warm_latencies: list[float] = []
        for _ in range(6):
            started = time.perf_counter()
            result = await client.decide_fast("测试一句", trace_id="bench-warm")
            warm_latencies.append((time.perf_counter() - started) * 1000)
            if not result.degraded:  # 连不通还不降级 = 硬故障
                return {"mode": "degrade", "error": "sidecar 不在却返回了非降级结果"}
    finally:
        await client.aclose()

    invariants = (
        cold.intent == "other"
        and cold.confidence == 0.0
        and cold.is_out_of_domain is True
        and cold.is_sensitive is False
    )
    # 熔断器连续 3 次失败就打开，之后是「立刻降级」（0.x ms）。两种耗时都是正确答案，
    # 但混在一起算 p50 会得出 0.1ms 这种没意义的数 → 分开报。
    # 真正"打到网络"的只有熔断前的几次（冷启那一次也算失败计数，故用耗时区分而不是切片）
    pre_breaker = [v for v in warm_latencies if v >= 5.0]
    post_breaker = [v for v in warm_latencies if v < 5.0]
    return {
        "mode": "degrade",
        "cold_ms": round(cold_ms, 1),
        "pre_breaker_ms": [round(v, 1) for v in pre_breaker],
        "pre_breaker_p50_ms": round(statistics.median(pre_breaker), 1),
        "post_breaker_p50_ms": round(statistics.median(post_breaker), 1) if post_breaker else None,
        "warm_p50_ms": round(statistics.median(pre_breaker), 1),
        "degraded": cold.degraded,
        "invariants_ok": invariants,
    }


async def run_pure(settings: Settings) -> dict[str, Any]:
    """纯前向模式（必须 jev 环境）：不含 HTTP，用来分离"模型慢"与"环境忙"。"""
    try:
        from app.system_one.backend import LayaBackend
    except Exception as exc:
        return {"mode": "pure", "error": f"无法导入后端（是否在主 .venv 里跑？）：{exc}"}

    backend = LayaBackend(settings)
    questions = build_fast_questions()
    load_started = time.perf_counter()
    try:
        await backend.ensure_loaded()
    except Exception as exc:
        return {"mode": "pure", "error": f"模型加载失败：{type(exc).__name__}: {exc}"}
    load_ms = (time.perf_counter() - load_started) * 1000

    latencies: list[float] = []
    for text in PROBE_TEXTS * 2:
        started = time.perf_counter()
        await backend.score(
            state={"asr_final": text, "current_task": "none", "media_status": "idle"},
            questions=questions,
        )
        latencies.append((time.perf_counter() - started) * 1000)

    return {
        "mode": "pure",
        "load_ms": round(load_ms, 1),
        "samples": len(latencies),
        "p50_ms": round(_pct(latencies, 0.50), 1),
        "p95_ms": round(_pct(latencies, 0.95), 1),
        "max_ms": round(max(latencies), 1),
    }


def _verdict(result: dict[str, Any]) -> tuple[bool, list[str]]:
    """把结果折成 PASS/FAIL + 人话结论（复测报告直接抄这几行）。"""
    notes: list[str] = []
    mode = result.get("mode")
    if "error" in result:
        return False, [f"❌ {result['error']}"]

    if mode == "http":
        ok = True
        if result["success_rate"] < SUCCESS_RATE_FLOOR:
            ok = False
            notes.append(
                f"❌ 成功率 {result['success_rate']:.0%} < {SUCCESS_RATE_FLOOR:.0%}"
                "（195ms 内部预算被挤爆：先看 pure 模式是否也慢，再查宿主 CPU）"
            )
        else:
            notes.append(f"✅ 成功率 {result['success_rate']:.0%}")
        if result["p95_ms"] > FAST_P95_BUDGET_MS:
            ok = False
            notes.append(f"❌ 快路径 p95 {result['p95_ms']}ms > {FAST_P95_BUDGET_MS}ms")
        else:
            notes.append(f"✅ 快路径 p95 {result['p95_ms']}ms ≤ {FAST_P95_BUDGET_MS}ms")
        return ok, notes

    if mode == "degrade":
        ok = result["degraded"] and result["invariants_ok"] and result["warm_p50_ms"] <= DEGRADE_BUDGET_MS
        notes.append(f"降级 degraded={result['degraded']} 三不变量={result['invariants_ok']}")
        notes.append(
            f"{'✅' if result['warm_p50_ms'] <= DEGRADE_BUDGET_MS else '❌'} "
            f"降级耗时 warm p50 {result['warm_p50_ms']}ms（冷启首次 {result['cold_ms']}ms）"
            f" ≤ {DEGRADE_BUDGET_MS}ms 判定用 warm"
        )
        return ok, notes

    if mode == "pure":
        ok = result["p95_ms"] <= FAST_P95_BUDGET_MS
        notes.append(
            f"{'✅' if ok else '❌'} 纯前向 p50 {result['p50_ms']}ms p95 {result['p95_ms']}ms"
            f"（加载 {result['load_ms']}ms / {result['samples']} 次）"
        )
        if not ok:
            notes.append(
                "   → 模型本身已超预算：查宿主 CPU 占用（IDE/浏览器/其它模型进程）与 torch 线程数"
            )
        return ok, notes

    return False, [f"❌ 未知模式：{mode}"]


#: 待落盘结果（async 侧只算，写盘在 sync 的 main() 完成）
_pending_payload: list[tuple[Path, dict[str, Any]] | None] = [None]


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    modes = ["http", "degrade"] if args.mode == "all" else [args.mode]
    results: list[dict[str, Any]] = []
    all_ok = True

    print(f"# sidecar 复测 · {time.strftime('%Y-%m-%d %H:%M:%S')}")
    for key, value in _env_report(settings).items():
        print(f"# {key}: {value}")

    for mode in modes:
        if mode == "http":
            result = await run_http(settings, args.requests)
        elif mode == "degrade":
            result = await run_degrade(settings)
        else:
            result = await run_pure(settings)
        ok, notes = _verdict(result)
        all_ok = all_ok and ok
        results.append({"ok": ok, **result})
        print(f"\n## {mode}")
        for note in notes:
            print(note)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.json_out:
        # 写盘放在 sync 的 main()（async 里做阻塞文件 IO 会卡事件循环）
        _pending_payload[0] = (Path(args.json_out), {"env": _env_report(settings), "results": results})

    print(f"\n=== 总判定：{'PASS' if all_ok else 'FAIL'} ===")
    return 0 if all_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="A2 延迟/降级复测（sidecar 健康、p95、降级）")
    parser.add_argument(
        "--mode",
        choices=("all", "http", "degrade", "pure"),
        default="all",
        help="all=http+degrade；pure 需在 jev 环境跑（要 torch）",
    )
    parser.add_argument("--requests", type=int, default=20, help="http 模式请求数（默认 20）")
    parser.add_argument(
        "--json-out",
        default="eval/artifacts/a2_latency.json",
        help="结果落盘路径（默认落不入库的 eval/artifacts/）",
    )
    args = parser.parse_args()
    code = asyncio.run(main_async(args))
    pending = _pending_payload[0]
    if pending is not None:
        target, payload = pending
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入：{target}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
