"""S2.0 形态门禁：G-3 加载自检 / G-4 七类意图可用性 / G-5 CPU 延迟。

用法（必须用装在独立环境里的 python，不要用项目 .venv）：
    D:\\Users\\Alienware\\anaconda3\\envs\\jev\\python.exe spikes/chinese_jev_hello/gate.py \
        --code-dir D:/models/laya --model-dir D:/models/laya/multilingual \
        --samples spikes/chinese_jev_hello/samples.jsonl \
        --out spikes/chinese_jev_hello/gate.json

纪律（对应计划 §5.2.1 与 §10）：
- 推理只用模型仓库自带的 `rl_agent_api.RLAgent`，**不 import pip 的 laya 包**（主理人要求）。
- 不调大任何超时；延迟超标只如实记录。
- 候选键用中文（Laya 是中文/多语言权重，讲义实测用中文键更贴题面），
  业务枚举仍是英文 Intent；两者由 KEY_TO_INTENT 显式映射，不做隐式猜测。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import traceback
from pathlib import Path

#: 模型输出键 → 业务 Intent 枚举（PRD 七类）。显式映射，杜绝"看着像就归它"。
KEY_TO_INTENT = {
    "闲聊陪伴": "chat",
    "用药确认": "medication",
    "戏曲点播": "opera",
    "电影点播": "movie",
    "天气查询": "weather",
    "健康采集": "health_checkup",
    "其他": "other",
}

#: 题面：一次前向并行四问。confidence 由 intent 概率分布派生，不单独出题。
QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "根据长者的这句话，判断它主要属于哪一类意图",
        "criteria": {
            "闲聊陪伴": "聊天、问候、倾诉心情、讲过去的事、问常识",
            "用药确认": "回答是否吃过药、询问自己的药还剩多少、确认用药安排",
            "戏曲点播": "要求播放、暂停、切换戏曲，或调整播放音量与进度",
            "电影点播": "要求看电影、放影片、找片子",
            "天气查询": "询问天气、温度、穿衣、是否下雨带伞",
            "健康采集": "要求记录或查询血压血糖体温等健康数据",
            "其他": "叫护工、联系家属、问日期时间等以上都不属于的请求",
        },
    },
    "is_sensitive": {
        "type": "noul",
        "instructions": "命题：长者这句话表达了需要立即处理的健康紧急情况（胸痛、呼吸困难、晕倒、剧烈疼痛、轻生念头）",
        "criteria": {
            "false": "没有表达上述紧急症状，可以按普通流程处理",
            "true": "表达了上述紧急症状之一，需要立即通知护工",
        },
    },
    "privacy_scope": {
        "type": "choice",
        "instructions": "判断这句话涉及的内容适合在什么范围内讨论",
        "criteria": {
            "personal": "涉及本人健康、用药、身体感受等私密内容",
            "shared": "同住长者都在场的公共话题，如天气、戏曲、活动",
            "none": "没有涉及隐私内容",
        },
    },
    "is_out_of_domain": {
        "type": "noul",
        "instructions": "命题：这句话超出了康养陪伴助手能处理的业务范围（如编程、法律诉讼、金融投资、外语翻译）",
        "criteria": {
            "false": "属于康养陪伴助手的业务范围",
            "true": "明显超出业务范围",
        },
    },
}


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code-dir", default="D:/models/laya")
    ap.add_argument("--model-dir", default="D:/models/laya/multilingual")
    ap.add_argument("--samples", default="spikes/chinese_jev_hello/samples.jsonl")
    ap.add_argument("--out", default="spikes/chinese_jev_hello/gate.json")
    ap.add_argument("--threads", type=int, default=2, help="torch CPU 线程数（与厂商校验脚本一致取 2）")
    ap.add_argument("--intent-only", action="store_true", help="只跑 intent 单问（等价 --only intent）")
    ap.add_argument("--only", default="", help="逗号分隔的问题子集，如 intent,is_sensitive；空=全部四问")
    args = ap.parse_args()

    import torch  # 延迟导入，--help 时不付代价

    torch.set_num_threads(args.threads)

    sys.path.insert(0, str(Path(args.code_dir).resolve()))
    from rl_agent_api import RLAgent  # noqa: PLC0415  仓库自带推理入口，非 pip laya

    samples = [json.loads(line) for line in Path(args.samples).read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"样例 {len(samples)} 条 | 线程 {args.threads} | 设备 cpu")

    # ---------- G-3 加载 ----------
    t0 = time.perf_counter()
    agent = RLAgent(args.model_dir, device="cpu")
    load_s = time.perf_counter() - t0
    cfg = agent.cfg
    print(f"[G-3] 加载完成 {load_s:.1f}s | max_len={cfg['max_len']} head_max_len={cfg['head_max_len']}")

    asked = {"intent": QUESTIONS["intent"]} if args.intent_only else QUESTIONS
    if args.only:
        wanted = [x.strip() for x in args.only.split(",") if x.strip()]
        unknown = [w for w in wanted if w not in QUESTIONS]
        if unknown:
            raise SystemExit(f"未知问题名 {unknown}，可选 {sorted(QUESTIONS)}")
        asked = {k: QUESTIONS[k] for k in wanted}
    first = agent.system_one(samples[0]["text"], asked)
    print("[G-3] 首条答案结构 keys:", sorted(first["answers"]))
    print("[G-3] intent 示例:", json.dumps(first["answers"]["intent"], ensure_ascii=False))

    # ---------- 预热（不计入延迟） ----------
    for _ in range(2):
        agent.system_one("预热：今天天气怎么样", asked)

    # ---------- G-4 准确率 + G-5 延迟 ----------
    rows, latencies = [], []
    for s in samples:
        t = time.perf_counter()
        try:
            res = agent.system_one(s["text"], asked)
            err = None
        except Exception as exc:  # noqa: BLE001  选项挤压/形状错误都要留证，不能吞
            res, err = None, f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        ms = (time.perf_counter() - t) * 1000
        latencies.append(ms)

        if res is None:
            rows.append({"id": s["id"], "text": s["text"], "gold": s["gold_intent"], "error": err, "ms": round(ms, 1)})
            continue
        ans = res["answers"]
        top = ans["intent"]["choice"]
        prob = ans["intent"]["probabilities"]
        rows.append({
            "id": s["id"],
            "text": s["text"],
            "gold": s["gold_intent"],
            "pred_key": top,
            "pred": KEY_TO_INTENT.get(top, "other"),
            "confidence": ans["intent"]["confidence"],
            "max_prob": max(prob.values()),
            "p_sensitive": ans["is_sensitive"]["noul"] if "is_sensitive" in ans else None,
            "ms": round(ms, 1),
        })

    ok = [r for r in rows if r.get("pred") == r["gold"]]
    correct = len(ok)
    total = len(rows)
    lat = [r["ms"] for r in rows]
    summary = {
        "model_dir": args.model_dir,
        "code": "rl_agent_api.RLAgent（仓库自带，未使用 pip laya）",
        "questions_asked": list(asked),
        "threads": args.threads,
        "load_seconds": round(load_s, 1),
        "samples": total,
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else None,
        "pass_15_of_20": correct >= 15,
        "latency_ms": {"p50": round(pct(lat, 0.50), 1), "p95": round(pct(lat, 0.95), 1), "max": round(max(lat), 1) if lat else None, "mean": round(statistics.fmean(lat), 1) if lat else None},
        "p95_within_200ms": pct(lat, 0.95) <= 200,
        "rows": rows,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\n{:<4} {:<8} {:<8} {:<7} {:>6} {:>8}".format("id", "gold", "pred", "conf", "ms", "ok"))
    for r in rows:
        print("{:<4} {:<8} {:<8} {:<7} {:>6} {:>8}".format(
            r["id"], r["gold"], r.get("pred", "ERR"), f"{r.get('confidence', 0):.2f}", r["ms"], "✓" if r.get("pred") == r["gold"] else "✗"))

    print(f"\n[G-4] 准确率 {correct}/{total} = {summary['accuracy']:.1%}（门槛 ≥15/20）→ {'过' if summary['pass_15_of_20'] else '不过'}")
    print(f"[G-5] p50={summary['latency_ms']['p50']}ms p95={summary['latency_ms']['p95']}ms max={summary['latency_ms']['max']}ms → {'≤200ms 达标' if summary['p95_within_200ms'] else '超 200ms，如实标红'}")
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
