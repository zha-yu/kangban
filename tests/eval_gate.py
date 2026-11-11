"""评测门禁：`tests/eval_gate.py`（Day 8 · D5 · 对应 PRD FR-TC-09 / §5.2）。

## 它判什么

三道门，任一不过 → `exit 1`：

| 门 | 判据 | 来源 |
| --- | --- | --- |
| A 敏感召回 | ≥ 95% | PRD §5.2 硬线（写死，**不随基线下降**） |
| B 各项不回退 | 普通意图 / 边界 / 模糊 各项 ≥ 首跑基线（`baseline_lock.json`） | 本计划 D5④「≥Day 7 路由基线不回退」 |
| C 行为场景 | 六类边界中 超时/打断/重复/越权 引用的既有测试全绿 | 照护台 §6.3、day08 计划 S3 |

## 它不做什么

- **不改任何阈值代码**：`rules.py` / `system_one` 的阈值一律只读；本文件只读取 `Settings` 做判定。
- **不拿 mock 冒充基线**：`--provider mock` 必须显式加 `--allow-mock`，且**永不写锁**、报告首行标「非基线」。
- **不复制评测实现**：`load_fixtures` / `evaluate` / `Summary` 一律从 `scripts/eval_router.py` import。

## 为什么默认 provider 不是 sidecar

sidecar 的内部硬预算是 195ms（= 客户端 200ms − 5ms 缓冲），而本机实测单问
p50 206ms/p95 221ms（2026-11-11，空机单跑），**结构性超预算**，HTTP 通道会
系统性 503。503 → 全量 fallback → 评测看到的不是模型能力而是传输失败。
所以基线必须用 `--provider local`（进程内、不经 HTTP）：

```bash
# 真基线（torch 只在 jev / spikes 的独立环境，不进主 .venv）
PYTHONPATH=. D:/Users/Alienware/anaconda3/envs/jev/python.exe tests/eval_gate.py --provider local

# 机制自检（不代表模型能力，绝不写锁）
uv run python tests/eval_gate.py --provider mock --allow-mock
```

## FR-TC-09 拦截验证

```bash
# 临时把阈值调低（只改环境变量，不改代码），门禁必须被拦下
SYSTEM_ONE_CONFIDENCE_THRESHOLD=0.30 PYTHONPATH=. <jev-python> tests/eval_gate.py --provider local
# 验完恢复（不设该变量即回 0.85），再跑一次确认回绿
```
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402

# `scripts/` 不是包（无 __init__.py），按路径 import 以免动脚本目录结构。
sys.path.insert(0, str(ROOT / "scripts"))
from eval_router import Row, Summary, evaluate, load_fixtures, pct, percentile  # noqa: E402

# ---------------------------------------------------------------------------
# 门禁线（只读声明，不参与计算）
# ---------------------------------------------------------------------------

#: PRD §5.2 硬线：敏感召回 ≥95%。**写死，基线再低也不许降**
SENSITIVE_FLOOR_PCT = 95.0

#: 行为场景类边界所引用的既有测试（不新造第二套评测实现）
SCENARIO_TESTS: tuple[str, ...] = (
    "tests/test_degrade_timeout.py",  # 超时/降级
    "tests/test_turn_service.py",  # 打断/轮次
    "tests/test_tool_executor.py",  # 工具超时与熔断
    "tests/test_api_workorders_family.py",  # 通知/越权（家属侧）
    "tests/test_elders_api.py",  # 范围 403
    "tests/test_security.py",  # 鉴权与越权
)

#: 七层归因的固定行（缺数据的层如实写「无数据」，不许留空糊过去）
ATTRIBUTION_LAYERS: tuple[tuple[str, str], ...] = (
    ("ASR", "`asr_confidence` 当前无埋点"),
    ("分类器", "`eval_router` 的 intent / confidence / route_layer"),
    ("规则层", "`route_layer=rule` 的命中与误判"),
    ("召回", "memory 注入相关失败（固定集不涉注入）"),
    ("LLM", "生成降级（本门禁不跑生成）"),
    ("Tool", "`tool_executions.error_class`（本门禁不执行工具）"),
    ("编排", "`conversation_events` 序列异常（本门禁只看二次轮动作）"),
)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_boundary_text(path: Path) -> list[Row]:
    """只取 `group=="boundary"` 的文本类子集。

    行为场景类（group=="scenario"）不喂 `eval_router`：它们判的是状态机行为，
    用意图打分只会得到「不敏感、不升级」这种恒真结论，看着绿实则无信息。
    """
    rows: list[Row] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw.get("group") != "boundary":
            continue
        rows.append(
            Row(
                id=raw["id"],
                group=raw["group"],
                text=raw["text"],
                gold_intent=raw.get("gold_intent", "other"),
                is_sensitive=bool(raw.get("is_sensitive", False)),
                note=raw.get("note", ""),
                media_status=raw.get("media_status", "idle"),
                current_task=raw.get("current_task", "none"),
            )
        )
    return rows


def failing_ids(rows: list[Row]) -> list[str]:
    """逐条判定「这一条算不算失败」，返回失败 id 列表（门 D 的条目级基线）。

    判据与 `scripts/eval_router.py` 的 `_tally` 保持同一口径，不另立一套：
    - normal：意图不等于 gold；
    - sensitive：既没被 G0 拦、也没被分类器判敏感；
    - boundary：被判敏感或动作升级（误杀）；
    - ambiguous：首次动作落到 tool/escalate，或二次轮没下沉 Agent。
    """
    fails: list[str] = []
    for row in rows:
        d = row.decision
        if row.group == "normal":
            bad = not row.intent_ok
        elif row.group == "sensitive":
            bad = not row.sensitive_ok
        elif row.group == "boundary":
            bad = not (d is not None and not d.is_sensitive and d.action != "escalate")
        elif row.group == "ambiguous":
            first_ok = d is not None and d.action in {"clarify", "agent"}
            bad = (not first_ok) or row.second_action != "agent"
        else:
            bad = d is None and row.error is not None
        if bad:
            fails.append(row.id)
    return sorted(fails)


def metrics_of(summary: Summary) -> dict[str, Any]:
    return {
        "normal_correct": summary.normal_correct,
        "normal_total": summary.normal_total,
        "normal_accuracy_pct": pct(summary.normal_correct, summary.normal_total),
        "sensitive_recalled": summary.sensitive_recalled,
        "sensitive_total": summary.sensitive_total,
        "sensitive_recall_pct": pct(summary.sensitive_recalled, summary.sensitive_total),
        "boundary_clean": summary.boundary_clean,
        "boundary_total": summary.boundary_total,
        "ambiguous_safe": summary.ambiguous_safe,
        "ambiguous_total": summary.ambiguous_total,
        "ambiguous_second_agent": summary.ambiguous_second_agent,
        "latency_p50_ms": percentile(summary.latencies, 0.50),
        "latency_p95_ms": percentile(summary.latencies, 0.95),
        "layers": dict(summary.layers),
    }


def run_scenarios(test_python: str) -> tuple[bool, str]:
    """跑行为场景引用的既有测试（子进程，不 import 测试模块）。"""
    missing = [t for t in SCENARIO_TESTS if not (ROOT / t).exists()]
    if missing:
        return False, f"场景测试文件缺失：{', '.join(missing)}"
    cmd = [test_python, "-m", "pytest", "-q", *SCENARIO_TESTS]
    # S603：cmd 全部由本文件常量拼出（测试文件路径写死、解释器来自 --test-python），无外部输入
    proc = subprocess.run(  # noqa: S603
        cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    return proc.returncode == 0, " | ".join(tail)


def attribute(rows: list[Row], summary: Summary) -> list[tuple[str, str]]:
    """按七层逐层给出「本次有没有数据 / 结论」。有数据就数，无数据就写无数据。"""
    by_group = Counter(row.group for row in rows)
    rule_hits = [r for r in rows if r.decision is not None and r.decision.route_layer == "rule"]
    rule_misfire = [
        r for r in rule_hits if r.group == "normal" and r.gold_intent != r.decision.intent
    ]
    so_rows = [r for r in rows if r.decision is not None and r.decision.route_layer == "system_one"]
    so_conf = [r.decision.confidence for r in so_rows if r.decision is not None]
    fallback = [r for r in rows if r.decision is not None and r.decision.route_layer == "fallback"]
    second_bad = [
        r
        for r in rows
        if r.group == "ambiguous" and r.second_action is not None and r.second_action != "agent"
    ]

    out: list[tuple[str, str]] = []
    for layer, source in ATTRIBUTION_LAYERS:
        if layer == "ASR":
            out.append((layer, "无数据（`asr_confidence` 未埋点；本门禁不跑 ASR）"))
        elif layer == "分类器":
            conf_txt = f"{min(so_conf):.2f}–{max(so_conf):.2f}" if so_conf else "无命中"
            out.append(
                (
                    layer,
                    f"system_one 命中 {len(so_rows)}/{summary.total}；confidence 区间 {conf_txt}；"
                    f"fallback {len(fallback)} 条",
                )
            )
        elif layer == "规则层":
            out.append(
                (
                    layer,
                    f"rule 命中 {len(rule_hits)} 条，其中普通组误判 {len(rule_misfire)} 条"
                    + ("（" + "、".join(r.id for r in rule_misfire) + "）" if rule_misfire else ""),
                )
            )
        elif layer == "召回":
            out.append((layer, "无数据（固定集不涉记忆注入；该层失败需 memory_service 侧证据）"))
        elif layer == "LLM":
            out.append((layer, "无数据（本门禁只测路由与安全，不跑生成）"))
        elif layer == "Tool":
            out.append((layer, "无数据（本门禁不执行工具；规则只验路由落点）"))
        elif layer == "编排":
            out.append(
                (
                    layer,
                    f"模糊组 {by_group.get('ambiguous', 0)} 条二次轮未下沉 Agent {len(second_bad)} 条"
                    + ("（" + "、".join(r.id for r in second_bad) + "）" if second_bad else ""),
                )
            )
        else:  # pragma: no cover - 层表写死，不会走到
            out.append((layer, source))
    return out


def verdict(ok: bool) -> str:
    return "✅ 通过" if ok else "❌ 不通过"


def write_report(
    path: Path,
    *,
    provider: str,
    command: str,
    metrics: dict[str, Any],
    lock: dict[str, Any] | None,
    lock_path: Path,
    gates: list[tuple[str, str, str, bool]],
    attribution: list[tuple[str, str]],
    bad_cases: list[Row],
    scenarios: tuple[bool, str] | None,
    fixtures_meta: dict[str, str],
) -> None:
    settings = get_settings()
    lines: list[str] = []
    lines.append("# Day 8 评测门禁报告（D5 · FR-TC-09）")
    lines.append("")
    lines.append(f"> 命令：`{command}`")
    lines.append(
        f"> provider：**{provider}**"
        + ("（⚠️ 非基线：mock 仅验机制）" if provider == "mock" else "（真权重，进程内，不经 HTTP）")
    )
    lines.append(
        f"> 版本：`{settings.system_one_model_version}` ｜ 置信阈值 {settings.system_one_confidence_threshold}"
        f" ｜ 锁文件 `{lock_path.name}`"
    )
    lines.append("")
    lines.append("## 1. 门禁判定")
    lines.append("")
    lines.append("| 门 | 结果 | 判据 | 结论 |")
    lines.append("| --- | --- | --- | --- |")
    for name, result, rule, ok in gates:
        lines.append(f"| {name} | {result} | {rule} | {verdict(ok)} |")
    lines.append("")
    lines.append("## 2. 指标")
    lines.append("")
    lines.append("| 指标 | 本次 | 锁内基线 | 说明 |")
    lines.append("| --- | --- | --- | --- |")
    lock_m = (lock or {}).get("metrics", {})

    def row(label: str, key: str, suffix: str = "") -> str:
        cur = metrics[key]
        base = lock_m.get(key)
        base_txt = "—（首跑无锁）" if base is None else f"{base}{suffix}"
        return f"| {label} | {cur}{suffix} | {base_txt} | |"

    lines.append(row("普通意图准确率", "normal_accuracy_pct", "%"))
    lines.append(row("敏感召回率", "sensitive_recall_pct", "%"))
    lines.append(
        f"| 安全边界（文本类） | {metrics['boundary_clean']}/{metrics['boundary_total']} | "
        f"{(str(lock_m.get('boundary_clean')) + '/' + str(lock_m.get('boundary_total'))) if lock_m else '—（首跑无锁）'} | "
        f"否定/多意图 |"
    )
    lines.append(
        f"| 模糊不误执行 | {metrics['ambiguous_safe']}/{metrics['ambiguous_total']} | "
        f"{(str(lock_m.get('ambiguous_safe')) + '/' + str(lock_m.get('ambiguous_total'))) if lock_m else '—（首跑无锁）'} | "
        f"不得 tool/escalate |"
    )
    lines.append(
        f"| 模糊二次轮下沉 | {metrics['ambiguous_second_agent']}/{metrics['ambiguous_total']} | "
        f"{(str(lock_m.get('ambiguous_second_agent')) + '/' + str(lock_m.get('ambiguous_total'))) if lock_m else '—（首跑无锁）'} | "
        f"只澄清一次 |"
    )
    lines.append(
        f"| 路由延迟 p50 / p95 | {metrics['latency_p50_ms']}ms / {metrics['latency_p95_ms']}ms | — | 进程内，不含 HTTP |"
    )
    layers = metrics["layers"]
    lines.append("")
    lines.append(
        f"> 四层分布：rule={layers.get('rule', 0)} / system_one={layers.get('system_one', 0)} / "
        f"agent={layers.get('agent', 0)} / fallback={layers.get('fallback', 0)}"
    )
    lines.append("")
    lines.append("## 3. 七层归因表")
    lines.append("")
    lines.append("| 层 | 数据来源 | 本次结论 |")
    lines.append("| --- | --- | --- |")
    for layer, text in attribution:
        source = next((s for name, s in ATTRIBUTION_LAYERS if name == layer), "")
        lines.append(f"| {layer} | {source} | {text} |")
    lines.append("")
    lines.append("## 4. bad case")
    lines.append("")
    if not bad_cases:
        lines.append("无新增 bad case。")
    else:
        lines.append("| id | 组 | 文本 | 期望 | 实际 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in bad_cases:
            d = r.decision
            actual = (
                f"{d.intent}/{d.route_layer}/{d.action}" if d is not None else (r.error or "无结果")
            )
            lines.append(f"| {r.id} | {r.group} | {r.text} | {r.gold_intent} | {actual} |")
        lines.append("")
        lines.append("> 已追加到 `eval/fixtures/badcase_backlog.jsonl`（回流留痕）。")
    lines.append("")
    lines.append("## 5. 行为场景（超时/打断/重复/越权）")
    lines.append("")
    if scenarios is None:
        lines.append("本次跳过（`--skip-scenarios`）。")
    else:
        ok, detail = scenarios
        lines.append(f"- 复用测试：{'、'.join(f'`{t}`' for t in SCENARIO_TESTS)}")
        lines.append(f"- 结果：{verdict(ok)} ｜ `{detail}`")
    lines.append("")
    lines.append("## 6. 样本指纹")
    lines.append("")
    lines.append("| 文件 | sha256 |")
    lines.append("| --- | --- |")
    for name, digest in fixtures_meta.items():
        lines.append(f"| `{name}` | `{digest}` |")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def append_backlog(path: Path, run_id: str, bad_cases: list[Row]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not bad_cases:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "run_id": run_id,
                        "date": time.strftime("%Y-%m-%d %H:%M"),
                        "summary": "无新增 bad case",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return 0
    with path.open("a", encoding="utf-8") as fh:
        for r in bad_cases:
            d = r.decision
            fh.write(
                json.dumps(
                    {
                        "run_id": run_id,
                        "date": time.strftime("%Y-%m-%d %H:%M"),
                        "id": r.id,
                        "group": r.group,
                        "text": r.text,
                        "expected": r.gold_intent,
                        "actual": f"{d.intent}/{d.route_layer}/{d.action}"
                        if d is not None
                        else (r.error or "无结果"),
                        "layer": (
                            "规则层"
                            if (d is not None and d.route_layer == "rule")
                            else "分类器"
                            if (d is not None and d.route_layer == "system_one")
                            else "编排"
                            if (d is not None and d.route_layer == "fallback")
                            else "无结果"
                        ),
                        "confidence": None if d is None else d.confidence,
                        "trace": "无（评测不入库，见 run_id）",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(bad_cases)


def self_check(fixtures: Path, boundary: Path) -> int:
    """pre-push 用的机制自检：**不跑模型、不写锁、不做判定**。

    它只回答一个问题：「样本还能被这道门禁正确加载吗」。答案是 no 时，
    说明 fixtures 被改坏了或六类边界缺类，钩子应当拦住推送——这与「跑分
    达不达标」是两件事，后者需要 torch 与 ~60s，不能塞进 pre-push。
    """
    problems: list[str] = []
    try:
        base = load_fixtures(fixtures)
    except Exception as exc:
        print(f"[gate] 自检失败：基线集加载异常 {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    try:
        btext = load_boundary_text(boundary)
        raw_boundary = [
            json.loads(line)
            for line in boundary.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as exc:
        print(f"[gate] 自检失败：边界集加载异常 {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    by_group = Counter(r.group for r in base)
    if len(base) != 110 or by_group.get("normal") != 50 or by_group.get("sensitive") != 20:
        problems.append(
            f"基线集应为 110 条（普通 50/敏感 20），实际 {len(base)} 条 {dict(by_group)}"
        )

    classes = Counter(r.get("boundary_class") for r in raw_boundary)
    need = ("negation", "multi_intent", "timeout", "barge_in", "duplicate", "scope")
    for name in need:
        if classes.get(name, 0) < 5:
            problems.append(f"边界类 {name} 只有 {classes.get(name, 0)} 条（要求 ≥5）")
    if not btext:
        problems.append("boundary_v1 里没有可进文本路由的子集（group=boundary）")

    ids = [r.id for r in base] + [r.get("id", "") for r in raw_boundary]
    if len(ids) != len(set(ids)):
        dup = [i for i, c in Counter(ids).items() if c > 1 and i]
        problems.append(f"id 重复：{dup[:5]}")

    if problems:
        print("[gate] 自检未通过：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(
        f"[gate] 自检通过：基线 {len(base)} 条（{dict(by_group)}）＋边界 {len(raw_boundary)} 条"
        f"（{len(need)} 类各 ≥5），id 唯一，文本子集 {len(btext)} 条"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Day 8 评测门禁（FR-TC-09）：只读阈值，不改评测实现")
    ap.add_argument("--fixtures", default=str(ROOT / "eval/fixtures/day7_intents.jsonl"))
    ap.add_argument("--boundary", default=str(ROOT / "eval/fixtures/boundary_v1.jsonl"))
    ap.add_argument(
        "--provider",
        choices=("local", "sidecar", "mock"),
        default=os.environ.get("EVAL_GATE_PROVIDER", "local"),
    )
    ap.add_argument(
        "--allow-mock", action="store_true", help="允许 mock 跑机制（永不写锁，报告标非基线）"
    )
    ap.add_argument("--lock", default=str(ROOT / "eval/reports/day8_baseline_lock.json"))
    ap.add_argument("--report", default=str(ROOT / "eval/reports/day8_eval_baseline.md"))
    ap.add_argument("--backlog", default=str(ROOT / "eval/fixtures/badcase_backlog.jsonl"))
    ap.add_argument(
        "--skip-scenarios", action="store_true", help="跳过行为场景测试（调试用，不用于门禁放行）"
    )
    ap.add_argument(
        "--test-python",
        default=os.environ.get("EVAL_GATE_TEST_PYTHON", str(ROOT / ".venv/Scripts/python.exe")),
        help="跑场景测试的解释器（默认项目 .venv）",
    )
    ap.add_argument(
        "--self-check",
        action="store_true",
        help="机制自检：只验样本可加载、六类齐、id 唯一；不跑模型、不写锁（pre-push 用）",
    )
    args = ap.parse_args()

    if args.self_check:
        return self_check(Path(args.fixtures), Path(args.boundary))

    if args.provider == "mock" and not args.allow_mock:
        print(
            "[gate] 拒绝执行：mock 不得冒充基线。若只验机制，请加 --allow-mock。", file=sys.stderr
        )
        return 2

    fixtures_path = Path(args.fixtures)
    boundary_path = Path(args.boundary)
    lock_path = Path(args.lock)

    rows = load_fixtures(fixtures_path)
    rows += load_boundary_text(boundary_path)

    started = time.perf_counter()
    # `evaluate` 是 async（内部走 httpx / to_thread），门禁本体是同步 CLI
    summary = asyncio.run(evaluate(rows, provider=args.provider))
    elapsed = time.perf_counter() - started
    metrics = metrics_of(summary)

    lock: dict[str, Any] | None = None
    if lock_path.exists():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))

    settings = get_settings()
    gates: list[tuple[str, str, str, bool]] = []

    # 门 A：敏感召回硬线
    ok_a = metrics["sensitive_recall_pct"] >= SENSITIVE_FLOOR_PCT
    gates.append(
        (
            "A 敏感召回",
            f"{metrics['sensitive_recalled']}/{metrics['sensitive_total']}（{metrics['sensitive_recall_pct']}%）",
            f"≥{SENSITIVE_FLOOR_PCT}%（PRD §5.2 硬线）",
            ok_a,
        )
    )

    # 门 B：各项不回退（有锁比锁，无锁按首跑生成）
    if lock is None:
        ok_b = True
        b_result = (
            "mock 不写锁，无可比基线" if args.provider == "mock" else "首跑，无锁可比（本次写入锁）"
        )
    else:
        lm = lock["metrics"]
        pairs = [
            ("普通意图", metrics["normal_accuracy_pct"], lm["normal_accuracy_pct"]),
            ("边界", metrics["boundary_clean"], lm["boundary_clean"]),
            ("模糊不误执行", metrics["ambiguous_safe"], lm["ambiguous_safe"]),
            ("模糊二次轮", metrics["ambiguous_second_agent"], lm["ambiguous_second_agent"]),
        ]
        regressed = [f"{name} {cur}<{base}" for name, cur, base in pairs if cur < base]
        ok_b = not regressed
        b_result = "无回退" if ok_b else "回退：" + "；".join(regressed)
    gates.append(("B 不回退", b_result, "各项 ≥ 锁内基线", ok_b))

    # 门 C：行为场景
    if args.skip_scenarios:
        scenarios: tuple[bool, str] | None = None
        ok_c, c_result = True, "已跳过（不计入门禁，仅调试）"
    else:
        if not Path(args.test_python).exists() and args.test_python != sys.executable:
            print(f"[gate] 场景测试解释器不存在：{args.test_python}", file=sys.stderr)
            return 2
        scenarios = run_scenarios(args.test_python)
        ok_c, c_result = scenarios[0], ("全绿" if scenarios[0] else "有失败：" + scenarios[1])
    gates.append(("C 行为场景", c_result, "超时/打断/重复/越权 相关测试全绿", ok_c))

    # 门 D：**不得新增误杀**（条目级，比"全对"更准也更不越权）
    # 为什么不是"全对"：Day 7 基线本身就有 4 条边界误判与 1 条模糊误判在案，
    # 要求全对等于把门禁线抬到比基线更高，等于私自加严——那是改验收，不是 D 的权限。
    # 所以只比「本次失败集合 − 锁内失败集合」，新增一条即拦。
    failing = failing_ids(rows)
    if lock is None:
        ok_d = True
        d_result = f"首跑，本次失败 {len(failing)} 条全部入锁作为既有项"
    else:
        base_failing = set(lock.get("failing_ids", []))
        new_fails = sorted(set(failing) - base_failing)
        ok_d = not new_fails
        d_result = (
            f"无新增误杀（既有 {len(base_failing)} 条）"
            if ok_d
            else f"新增误杀 {len(new_fails)} 条：" + "、".join(new_fails)
        )
    gates.append(
        (
            "D 无新增误杀",
            d_result,
            "本次失败项 ⊆ 锁内失败项（只拦新增，不改既有验收）",
            ok_d,
        )
    )

    fixtures_meta = {
        fixtures_path.name: sha256_of(fixtures_path),
        boundary_path.name: sha256_of(boundary_path),
    }

    run_id = time.strftime("%Y%m%d-%H%M%S")
    command = " ".join([Path(sys.executable).name, "tests/eval_gate.py", *sys.argv[1:]])
    attribution = attribute(rows, summary)
    write_report(
        Path(args.report),
        provider=args.provider,
        command=command,
        metrics=metrics,
        lock=lock,
        lock_path=lock_path,
        gates=gates,
        attribution=attribution,
        bad_cases=summary.bad_cases,
        scenarios=scenarios,
        fixtures_meta=fixtures_meta,
    )
    added = append_backlog(Path(args.backlog), run_id, summary.bad_cases)

    all_ok = all(ok for *_, ok in gates)
    if all_ok and args.provider != "mock":
        lock_payload = {
            "version": 1,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_id": run_id,
            "provider": args.provider,
            "model_version": settings.system_one_model_version,
            "confidence_threshold": settings.system_one_confidence_threshold,
            "fixtures": fixtures_meta,
            "metrics": metrics,
            "failing_ids": failing,
            "command": command,
        }
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock is None:
            lock_path.write_text(
                json.dumps(lock_payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            print(f"[gate] 首跑基线已写入锁：{lock_path}")
        else:
            # 只升不降：指标变好时刷新锁，变差时保留旧锁并把失败留在报告里
            improved = metrics["normal_accuracy_pct"] > lock["metrics"]["normal_accuracy_pct"]
            if improved:
                lock_path.write_text(
                    json.dumps(lock_payload, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                print("[gate] 指标提升，锁已刷新（只升不降）")

    print(
        f"[gate] provider={args.provider} 普通 {metrics['normal_correct']}/{metrics['normal_total']}"
        f"（{metrics['normal_accuracy_pct']}%）｜敏感 {metrics['sensitive_recalled']}/{metrics['sensitive_total']}"
        f"（{metrics['sensitive_recall_pct']}%）｜边界 {metrics['boundary_clean']}/{metrics['boundary_total']}"
        f"｜p50={metrics['latency_p50_ms']}ms p95={metrics['latency_p95_ms']}ms ｜耗时 {elapsed:.1f}s"
    )
    for name, result, rule, ok in gates:
        print(f"[gate] {verdict(ok)} {name}：{result} ｜判据 {rule}")
    print(f"[gate] badcase 回流 {added} 条 → {args.backlog}")
    print(f"[gate] 报告 → {args.report}")
    if args.provider == "mock":
        print("[gate] 注意：mock 结果不代表模型能力，未写锁。", file=sys.stderr)
    print("[gate] RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
