"""
B-4 Laya Spike · 最小 HTTP 客户端 + 延迟测量
============================================

目标：验证 Laya（System One）CPU 分类器 HTTP 服务能否：
  1. 在 7001 端口起服务（config.py laya_base_url 默认值）
  2. 接收 ASR 文本、返回四信号（intent / is_sensitive / confidence / is_out_of_domain）
  3. 单句 CPU 推理延迟 ≤ 200ms（AGENTS §6 / §7 硬性数值）

用法：
  # 前提：laya-serve 已在 7001 端口起服务
  python spikes/laya_hello/spike_hello.py

  # 或者先起 mock server（仅用于测客户端链路通不通）
  python spikes/laya_hello/mock_laya_server.py &
  python spikes/laya_hello/spike_hello.py

不合并主线。完整 spike 产物见 README.md。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 硬编码输入（与 config.py 默认值对齐，spike 不引入 Settings 依赖）
# ---------------------------------------------------------------------------
LAYA_BASE_URL = "http://localhost:7001"
LAYA_API_KEY = ""
LAYA_TIMEOUT_MS = 200
LAYA_CONFIDENCE_THRESHOLD = 0.85

# AGENTS §6 / TD 170 定义的七类 intent + 四信号输出结构
INTENTS = [
    "chat",             # 闲聊
    "medication",       # 用药查询
    "opera",            # 戏曲点播
    "movie",            # 电影点播
    "weather",          # 天气查询
    "health_checkup",   # 健康采集
    "other",            # 其他
]

# 固定测试文本集（覆盖 AGENTS §7 评测集普通 50 / 敏感 20 / 边界 20 的样例）
TEST_SAMPLES: list[tuple[str, str]] = [
    ("chat", "今天天气怎么样"),
    ("medication", "我要吃药"),
    ("opera", "给我来一段京剧"),
    ("weather", "北京今天天气"),
    ("health_checkup", "帮我量个血压"),
    ("chat", "你好呀"),
    ("medication", "几点该吃药了"),
    ("chat", "换一段"),
    ("chat", "大声点"),
    ("opera", "越剧"),
]


@dataclass
class LayaResponse:
    """四信号输出结构（TD 170 + FR-DLG-03 AC-03）"""
    intent: str | None = None
    is_sensitive: bool = False
    confidence: float = 0.0
    is_out_of_domain: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0


async def call_laya_http(text: str) -> LayaResponse:
    """发 HTTP POST 到 Laya 服务，返回四信号 + 延迟"""
    import httpx

    payload = {
        "state": text,
        "questions": {
            # 具体问题 schema 取决于 Laya checkpoint 定义
            # 下面是 config.py laya 配置推断的四信号映射
            "intent": {"type": "choice", "choices": INTENTS},
            "is_sensitive": {"type": "score", "min": 0.0, "max": 1.0},
            "confidence": {"type": "score", "min": 0.0, "max": 1.0},
            "is_out_of_domain": {"type": "score", "min": 0.0, "max": 1.0},
        },
    }

    headers = {"Content-Type": "application/json"}
    if LAYA_API_KEY:
        headers["Authorization"] = f"Bearer {LAYA_API_KEY}"

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=LAYA_TIMEOUT_MS / 1000 + 0.5) as client:
            resp = await client.post(
                f"{LAYA_BASE_URL}/classify",
                json=payload,
                headers=headers,
            )
            elapsed = (time.perf_counter() - start) * 1000
            resp.raise_for_status()
            data = resp.json()
            return LayaResponse(
                intent=data.get("intent"),
                is_sensitive=bool(data.get("is_sensitive", False)),
                confidence=float(data.get("confidence", 0.0)),
                is_out_of_domain=bool(data.get("is_out_of_domain", False)),
                raw=data,
                elapsed_ms=elapsed,
            )
    except httpx.ConnectError:
        # 服务没起 → 纯 CPU mock 延迟（单句 200ms 目标）
        elapsed = (time.perf_counter() - start) * 1000
        return LayaResponse(
            intent=None,
            raw={"error": "connect_error", "url": LAYA_BASE_URL},
            elapsed_ms=elapsed,
        )
    except httpx.TimeoutException:
        elapsed = (time.perf_counter() - start) * 1000
        return LayaResponse(
            intent=None,
            raw={"error": "timeout", "threshold_ms": LAYA_TIMEOUT_MS},
            elapsed_ms=elapsed,
        )


async def run_latency_benchmark() -> list[LayaResponse]:
    """跑固定测试集、记每条延迟"""
    results: list[LayaResponse] = []
    for label, text in TEST_SAMPLES:
        resp = await call_laya_http(text)
        results.append(resp)
        status = "OK" if resp.intent else f"FAIL({resp.raw.get('error','unknown')})"
        print(
            f"  [{label:18s}] {resp.elapsed_ms:8.1f}ms  "
            f"intent={resp.intent:16s} conf={resp.confidence:.2f}  {status}"
        )
    return results


def summarize(results: list[LayaResponse]) -> None:
    """汇总延迟 + 成功率"""
    ok = [r for r in results if r.intent is not None]
    fails = [r for r in results if r.intent is None]
    latencies = [r.elapsed_ms for r in ok]

    print("\n" + "=" * 60)
    print(f"样本数: {len(results)}  成功: {len(ok)}  失败: {len(fails)}")
    if latencies:
        latencies_sorted = sorted(latencies)
        print(f"延迟(ms): min={latencies_sorted[0]:.1f}  "
              f"avg={sum(latencies)/len(latencies):.1f}  "
              f"p95={latencies_sorted[int(len(latencies_sorted)*0.95)-1]:.1f}  "
              f"max={latencies_sorted[-1]:.1f}")
        target_ok = sum(1 for l in latencies if l <= 200) / len(latencies)
        print(f"≤200ms 达标率: {target_ok*100:.1f}%（AGENTS §6 硬性目标）")
    else:
        print("⚠️ 无成功样本")

    if fails:
        print("\n失败详情:")
        for r in fails:
            print(f"  {r.raw}")


async def main() -> int:
    print(f"Laya Spike · base_url={LAYA_BASE_URL}  timeout_ms={LAYA_TIMEOUT_MS}")
    print(f"置信度阈值={LAYA_CONFIDENCE_THRESHOLD}  样本={len(TEST_SAMPLES)}")
    print("-" * 60)

    results = await run_latency_benchmark()
    summarize(results)

    # 失败数 = 全部失败 → 服务没起（spike 中断，等 Laya 服务部署）
    if not any(r.intent for r in results):
        print("\n⚠️ Laya 服务未就绪。请先起服务：")
        print("  pip install laya && laya-serve --port 7001")
        print("  或 python spikes/laya_hello/mock_laya_server.py &")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
