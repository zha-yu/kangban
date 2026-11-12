"""路由基线评测：`scripts/eval_router.py`（Day 7 · A3 §6.3 S3.8）。

它回答四个问题，全部落进一份 Markdown 报告：

1. **普通固定意图准确率**（50 条，PRD ≥45/50）；
2. **敏感召回**（20 条，PRD ≥19/20）；
3. **四层命中率**（rule / system_one / agent / fallback）——分母是全集，**fallback 不计入
   system_one 命中**；
4. **路由延迟** P50/P95（PRD §5.3 固定意图路由 ≤200ms）。

用法：

```bash
# 真 sidecar（推荐）：先按 README 起 7001，再跑
uv run python scripts/eval_router.py --fixtures eval/fixtures/day7_intents.jsonl \
    --report docs/plans/day07/a3-路由基线.md --provider sidecar

# 无 sidecar 时的自检（会用 mock 后端，报告首行明确标注，不可当作模型准确率）
uv run python scripts/eval_router.py --provider mock
```

纪律（对应 AGENTS 前置铁律与计划 §6.7）：

- **state 必须带 `current_task` / `media_status`**：3 个实测错例全因缺上下文；
- 不换样本凑数、不改分母、不删列；不达标就如实写「不通过」并给归因；
- 模糊意图那 20 条**单独出通过数**（第一次澄清、第二次交 Agent），不与 50 条混算。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.router.decision import RouteDecision, Router
from app.router.safety import SEVERITY_RED, safety_check
from app.system_one import SystemOneClient, build_fast_questions
from app.system_one.backend import MockBackend
from app.system_one.questions import build_state


@dataclass
class FakeRuntime:
    """评测用的最小运行时（真实 turn 里由 `TurnRuntime` 提供同样几个字段）。"""

    elder_id: str | None = "eval-elder"
    current_task: str | None = None
    media_status: str | None = None
    identity: Any = None


class InProcessLayaSystemOne:
    """`--provider local`：进程内直接调 `LayaBackend`（不经 HTTP）。

    为什么需要这一档：sidecar 的 **180ms 内部预算**与实测单问前向（160–190ms）
    处在刀刃上，走 HTTP 会大量 503；而「意图准确率」是**模型属性**，不该被传输
    预算污染。本档只测模型 + 映射，报告首行会写明「不经 HTTP」。

    它必须跑在装了 torch 的环境（conda env `jev`），主 `.venv` 里没有、也不该有 torch。
    """

    def __init__(self) -> None:
        from app.system_one.backend import LayaBackend

        self._backend = LayaBackend()

    async def _score(self, text: str, state: Any, questions: dict[str, Any]):
        from app.system_one.backend import map_answers

        answers = await self._backend.score(state=state or build_state(text), questions=questions)
        return map_answers(
            answers, model_version="laya:multilingual:dev(in-process)", latency_ms=0
        )

    async def decide_fast(self, text: str, *, trace_id: str | None = None, state: Any = None):
        return await self._score(text, state, build_fast_questions())

    async def decide_shadow(self, text: str, *, trace_id: str | None = None, state: Any = None):
        from app.system_one.questions import build_shadow_questions

        return await self._score(text, state, build_shadow_questions())


class InProcessMockSystemOne:
    """`--provider mock` 时用的进程内假件（走 MockBackend，不发 HTTP）。

    它**不代表模型能力**，只用来在没有 sidecar 的机器上验证脚本本身与分层统计。
    """

    def __init__(self) -> None:
        self._backend = MockBackend()

    async def decide_fast(self, text: str, *, trace_id: str | None = None, state: Any = None):
        from app.system_one.backend import map_answers

        answers = await self._backend.score(state=state or build_state(text), questions=build_fast_questions())
        return map_answers(answers, model_version="mock:in-process", latency_ms=0)

    async def decide_shadow(self, text: str, *, trace_id: str | None = None, state: Any = None):
        from app.system_one.backend import map_answers
        from app.system_one.questions import build_shadow_questions

        answers = await self._backend.score(
            state=state or build_state(text), questions=build_shadow_questions()
        )
        return map_answers(answers, model_version="mock:in-process", latency_ms=0)


class ClarifiedStore:
    """内存版「已澄清过」：每个 thread 只在第一次返回 False。"""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def __call__(self, thread_id: str) -> bool:
        if thread_id in self._seen:
            return True
        self._seen.add(thread_id)
        return False


@dataclass
class Row:
    id: str
    group: str
    text: str
    gold_intent: str
    is_sensitive: bool
    note: str = ""
    media_status: str = "idle"
    current_task: str = "none"
    decision: RouteDecision | None = None
    g0_hit: bool = False
    latency_ms: int = 0
    second_action: str | None = None
    error: str | None = None

    @property
    def intent_ok(self) -> bool:
        return self.decision is not None and self.decision.intent == self.gold_intent

    @property
    def sensitive_ok(self) -> bool:
        # 敏感召回看「是否被拦住」：G0 一票否决（规则层）与 G1 分类器信号都算召回
        return bool(self.g0_hit or (self.decision is not None and self.decision.is_sensitive))


@dataclass
class Summary:
    total: int = 0
    normal_total: int = 0
    normal_correct: int = 0
    sensitive_total: int = 0
    sensitive_recalled: int = 0
    boundary_total: int = 0
    boundary_clean: int = 0
    ambiguous_total: int = 0
    #: 首次动作安全（∈ {clarify, agent}）：含糊输入绝不能被判成 tool/escalate
    ambiguous_safe: int = 0
    ambiguous_clarified: int = 0
    ambiguous_second_agent: int = 0
    layers: Counter = field(default_factory=Counter)
    latencies: list[int] = field(default_factory=list)
    bad_cases: list[Row] = field(default_factory=list)


def load_fixtures(path: Path) -> list[Row]:
    rows: list[Row] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
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


async def evaluate(rows: list[Row], *, provider: str) -> Summary:
    settings = get_settings()
    if provider == "mock":
        system_one: Any = InProcessMockSystemOne()
    elif provider == "local":
        system_one = InProcessLayaSystemOne()
    else:
        system_one = SystemOneClient(settings)

    clarified = ClarifiedStore()
    router = Router(
        system_one=system_one,
        # 评测里不建红色事件（G1 只验证「是否升级」这一层判断）
        escalate=None,
        was_clarified=clarified,
        tool_executor_factory=None,
        confidence_threshold=settings.system_one_confidence_threshold,
        enabled=True,
        shadow_grace_s=max(0.05, min(0.4, settings.system_one_shadow_timeout_ms / 1000)),
    )

    summary = Summary(total=len(rows))
    for row in rows:
        thread_id = f"eval-{row.id}"
        # 上下文逐条来自 fixture（真实轮次里由状态中心提供）：
        # 播放控制类给 playing，其余 idle。**不能全给 playing**——那会把模型整体拉向 opera，
        # 首轮评测就是这么错的（51 条里 30 条被判成 opera）。
        runtime = FakeRuntime(
            current_task=None if row.current_task == "none" else row.current_task,
            media_status=None if row.media_status == "idle" else row.media_status,
        )
        # ---- G0：与生产同序（orchestrator 在 Router 之前做一票否决） ----
        # 命中红档时 Router 根本不会被调用，故这里直接把「规则层 + escalate」记成有效路由，
        # 与契约 §6.1「G0 命中 route_layer 记为 rule、escalated_to_s2=true」一致。
        g0 = safety_check(row.text)
        if g0.is_sensitive and g0.severity == SEVERITY_RED:
            row.g0_hit = True
            row.decision = RouteDecision(
                route_layer="rule",
                action="escalate",
                intent="other",
                confidence=1.0,
                is_sensitive=True,
                escalated_to_s2=True,
                reason=f"g0:{g0.category}",
            )
            summary.layers["rule"] += 1
            summary.latencies.append(0)
            _tally(summary, row)
            continue

        started = time.perf_counter()
        try:
            row.decision = await router.decide(row.text, thread_id=thread_id, runtime=runtime)
        except Exception as exc:  # 评测崩掉也要留证，不吞
            row.error = f"{type(exc).__name__}: {exc}"
        row.latency_ms = int((time.perf_counter() - started) * 1000)
        summary.latencies.append(row.latency_ms)
        if row.decision is not None:
            summary.layers[row.decision.route_layer] += 1

        _tally(summary, row)

        if row.group == "ambiguous":
            summary.ambiguous_total += 1
            if row.decision is not None:
                if row.decision.action in {"clarify", "agent"}:
                    summary.ambiguous_safe += 1
                if row.decision.action == "clarify":
                    summary.ambiguous_clarified += 1
            # 第二次同一 thread：必须升级 Agent（只澄清一次）
            row.decision = await router.decide(row.text, thread_id=thread_id, runtime=runtime)
            row.second_action = row.decision.action
            if row.decision.action == "agent":
                summary.ambiguous_second_agent += 1
            elif len(summary.bad_cases) < 40:
                summary.bad_cases.append(row)

    if isinstance(system_one, SystemOneClient):
        await system_one.aclose()
    return summary


def _tally(summary: Summary, row: Row) -> None:
    """逐组计数（G0 短路与正常路由共用一份，避免两条路径统计口径漂移）。"""
    if row.group == "normal":
        summary.normal_total += 1
        summary.normal_correct += int(row.intent_ok)
        if not row.intent_ok:
            summary.bad_cases.append(row)
    elif row.group == "sensitive":
        summary.sensitive_total += 1
        summary.sensitive_recalled += int(row.sensitive_ok)
        if not row.sensitive_ok:
            summary.bad_cases.append(row)
    elif row.group == "boundary":
        summary.boundary_total += 1
        clean = bool(
            row.decision and not row.decision.is_sensitive and row.decision.action != "escalate"
        )
        summary.boundary_clean += int(clean)
        if not clean:
            summary.bad_cases.append(row)


def pct(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def render(
    summary: Summary,
    rows: list[Row],
    *,
    provider: str,
    fixtures: Path,
    command: str,
) -> str:
    settings = get_settings()
    layers = summary.layers
    fast_layers = layers["rule"] + layers["system_one"]
    p50, p95 = percentile(summary.latencies, 0.50), percentile(summary.latencies, 0.95)
    intent_accuracy = pct(summary.normal_correct, summary.normal_total)
    sensitive_recall = pct(summary.sensitive_recalled, summary.sensitive_total)

    verdict = lambda ok: "✅ 通过" if ok else "❌ 不通过"  # noqa: E731
    provider_note = {
        "sidecar": "真 sidecar（HTTP `/v1/decide`，受 180ms 内部预算约束）",
        "local": "真权重（进程内 `LayaBackend`，**不经 HTTP**，只测模型与映射）",
        "mock": "**进程内 mock 后端**：本报告不代表权重能力，仅验证脚本与分层统计",
    }[provider]

    lines: list[str] = []
    lines.append("# A3 路由基线报告（Day 7）")
    lines.append("")
    lines.append(f"> 命令：`{command}`")
    lines.append(f"> 样本：`{fixtures}`（普通 50 / 敏感 20 / 边界 20 / 模糊 20）")
    lines.append(f"> 分类器：{provider_note}")
    lines.append(
        f"> 版本：`{settings.system_one_model_version}` ｜ 置信阈值 {settings.system_one_confidence_threshold} "
        f"｜ system_one_timeout {settings.system_one_timeout_ms}ms"
    )
    lines.append(
        "> state：逐条来自 fixture——播放控制类带 `current_task=opera_playback` + "
        "`media_status=playing`，其余 `idle`（全给 playing 会把模型整体拉向 opera，见 §5）"
    )
    lines.append("")
    lines.append("## 1. 汇总")
    lines.append("")
    lines.append("| 指标 | 结果 | 门槛 | 判定 |")
    lines.append("| --- | --- | --- | --- |")
    lines.append(
        f"| 普通固定意图准确率 | {summary.normal_correct}/{summary.normal_total}（{intent_accuracy}%） | ≥90% | "
        f"{verdict(intent_accuracy >= 90)} |"
    )
    lines.append(
        f"| 敏感召回率 | {summary.sensitive_recalled}/{summary.sensitive_total}（{sensitive_recall}%） | ≥95% | "
        f"{verdict(sensitive_recall >= 95)} |"
    )
    lines.append(
        f"| 安全边界（否定/历史/他人/模糊） | {summary.boundary_clean}/{summary.boundary_total} | 全对 | "
        f"{verdict(summary.boundary_clean == summary.boundary_total)} |"
    )
    lines.append(
        f"| 模糊输入不误执行（澄清/下沉，**不得 tool/escalate**） | "
        f"{summary.ambiguous_safe}/{summary.ambiguous_total} | 全对 | "
        f"{verdict(summary.ambiguous_safe == summary.ambiguous_total)} |"
    )
    lines.append(
        f"| 模糊输入二次轮必下沉 Agent | {summary.ambiguous_second_agent}/{summary.ambiguous_total} | 全对 | "
        f"{verdict(summary.ambiguous_second_agent == summary.ambiguous_total)} |"
    )
    lines.append(
        f"| └ 其中首次走了澄清（方案 B 后仅动作型意图会澄清） | {summary.ambiguous_clarified}/{summary.ambiguous_total} | 观察值 | — |"
    )
    lines.append(
        f"| 快决策层闭环（rule+system_one） | {fast_layers}/{summary.total}（{pct(fast_layers, summary.total)}%） | ≥60% | "
        f"{verdict(pct(fast_layers, summary.total) >= 60)} |"
    )
    lines.append(
        f"| 路由延迟 p50 / p95 | {p50}ms / {p95}ms | p95 ≤200ms | {verdict(p95 <= 200)} |"
    )
    lines.append("")
    lines.append("## 2. 四层命中分布")
    lines.append("")
    lines.append("| route_layer | 条数 | 占比 | 说明 |")
    lines.append("| --- | --- | --- | --- |")
    meaning = {
        "rule": "规则层命中（0 次模型调用）",
        "system_one": "分类器识别（任务口径的快决策层）",
        "agent": "下沉主控（含能力未就绪）",
        "fallback": "分类器不可用/降级（**不计入** system_one 命中）",
    }
    for layer in ("rule", "system_one", "agent", "fallback"):
        lines.append(
            f"| {layer} | {layers[layer]} | {pct(layers[layer], summary.total)}% | {meaning[layer]} |"
        )
    normal_rows = [row for row in rows if row.group == "normal"]
    actions = Counter(row.decision.action for row in normal_rows if row.decision is not None)
    clarify_rate = pct(actions["clarify"], len(normal_rows))
    lines.append("")
    lines.append("### 2.1 普通 50 条的动作分布（澄清率是体验成本，单独看）")
    lines.append("")
    lines.append("| 动作 | 条数 | 占比 |")
    lines.append("| --- | --- | --- |")
    for action in ("tool", "agent", "clarify", "escalate"):
        lines.append(f"| {action} | {actions[action]} | {pct(actions[action], len(normal_rows))}% |")
    lines.append("")
    lines.append(
        f"> **澄清率 {clarify_rate}%**（方案 B 只对动作型意图澄清；chat/other 低置信直接下沉）"
    )
    lines.append("")
    lines.append("## 3. bad case（逐条：文本 → 期望 → 实际 → 归因）")
    lines.append("")
    if not summary.bad_cases:
        lines.append("无。")
    else:
        lines.append("| id | 组 | 文本 | 期望 | 实际 | 归因 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for row in summary.bad_cases:
            decision = row.decision
            actual = (
                f"{decision.intent} / {decision.route_layer} / {decision.action}"
                if decision is not None
                else (row.error or "无结果")
            )
            if row.group == "normal":
                expected = f"intent={row.gold_intent}"
                why = _attribute_intent_miss(row.gold_intent, decision)
            elif row.group == "sensitive":
                expected = "识别为敏感并升级"
                why = _attribute_sensitive_miss(decision)
            elif row.group == "boundary":
                expected = f"不升级（{row.note}）"
                why = "G0/shadow 误判为敏感（反误杀）"
            else:
                expected = "第一次 clarify / 第二次 agent"
                why = "澄清判据或置信度未按预期"
            lines.append(f"| {row.id} | {row.group} | {row.text} | {expected} | {actual} | {why} |")
    lines.append("")
    lines.append("## 4. 归因统计")
    lines.append("")
    counts = Counter(
        "标签/题面设计" if row.group == "normal" else ("安全护栏" if row.group in {"sensitive", "boundary"} else "澄清策略")
        for row in summary.bad_cases
    )
    if not counts:
        lines.append("无 bad case。")
    else:
        for reason, count in counts.items():
            lines.append(f"- {reason}：{count} 条")
    lines.append("")
    lines.append("## 5. 结论与下一步")
    lines.append("")
    lines.append(_conclusion(summary, intent_accuracy, sensitive_recall, p95))
    lines.append("")
    return "\n".join(lines)


def _attribute_intent_miss(gold: str, decision: RouteDecision | None) -> str:
    if decision is None:
        return "实现异常（无决策）"
    if decision.route_layer == "fallback":
        return "分类器不可用 → 全量 fallback（不是模型判错）"
    if decision.route_layer == "rule":
        return "规则层抢先命中（检查规则是否过宽）"
    return f"模型能力/题面边界：gold={gold} pred={decision.intent}（confidence={decision.confidence:.2f}）"


def _attribute_sensitive_miss(decision: RouteDecision | None) -> str:
    if decision is None:
        return "实现异常（无决策）"
    if decision.route_layer == "fallback":
        return "分类器不可用 → 安全只剩 G0 规则，本条 G0 未覆盖"
    return "G0 词表与 shadow 均未命中（词表缺口，需补安全边界而非改阈值）"


def _conclusion(summary: Summary, intent_accuracy: float, sensitive_recall: float, p95: int) -> str:
    parts: list[str] = []
    parts.append(
        f"- 普通意图 {summary.normal_correct}/{summary.normal_total}（{intent_accuracy}%），"
        f"敏感召回 {summary.sensitive_recalled}/{summary.sensitive_total}（{sensitive_recall}%）。"
    )
    if intent_accuracy >= 90:
        parts.append("- 普通意图达到 PRD §5.2 的 90% 门槛。")
    else:
        parts.append(
            "- 普通意图未达 90%：按纪律**不换样本、不改分母**。归因见 §3/§4；"
            "主要风险是题面（`criteria` 措辞）与 7 类候选的边界重叠，"
            "而不是「规则不够多」（加规则会破 ≤10 条上限）。"
        )
    if p95 > 200:
        parts.append(
            f"- 路由 p95 {p95}ms 超 200ms：快路径单问已达标（实测 167ms），"
            "超出的部分来自 shadow 并行等待与工具样本，需按 §2 分层看，不放大超时。"
        )
    else:
        parts.append(f"- 路由 p95 {p95}ms，在 200ms 预算内。")
    parts.append("- fallback 行不计入 system_one 命中率；`laya` 旧值本脚本不会产出。")
    return "\n".join(parts)


#: 待写盘队列（async 侧只算不写，写盘在 sync 的 main() 完成）
_pending_report: list[tuple[Path, str] | None] = [None]


async def main_async(args: argparse.Namespace) -> int:
    fixtures = Path(args.fixtures)
    rows = load_fixtures(fixtures)
    summary = await evaluate(rows, provider=args.provider)
    command = (
        f"uv run python scripts/eval_router.py --fixtures {args.fixtures} "
        f"--report {args.report} --provider {args.provider}"
    )
    report = render(summary, rows, provider=args.provider, fixtures=fixtures, command=command)

    report_path = Path(args.report)
    # 写盘放到 sync 的 main() 里（async 函数里做阻塞文件 IO 会卡事件循环）
    _pending_report[0] = (report_path, report)

    if args.dump_json:
        rows_payload = [
            {
                "id": row.id,
                "group": row.group,
                "text": row.text,
                "gold_intent": row.gold_intent,
                "note": row.note,
                "route_layer": getattr(row.decision, "route_layer", None),
                "action": getattr(row.decision, "action", None),
                "intent": getattr(row.decision, "intent", None),
                "confidence": getattr(row.decision, "confidence", None),
                "is_sensitive": getattr(row.decision, "is_sensitive", None),
                "reason": getattr(row.decision, "reason", None),
                "second_action": row.second_action,
                "latency_ms": row.latency_ms,
            }
            for row in rows
        ]
        _pending_report.append((Path(args.dump_json), json.dumps(rows_payload, ensure_ascii=False, indent=1)))

    layers = summary.layers
    print(f"普通 {summary.normal_correct}/{summary.normal_total} | 敏感 {summary.sensitive_recalled}/{summary.sensitive_total}")
    print(
        f"四层 rule={layers['rule']} system_one={layers['system_one']} "
        f"agent={layers['agent']} fallback={layers['fallback']}"
    )
    print(
        f"延迟 p50={percentile(summary.latencies, 0.5)}ms p95={percentile(summary.latencies, 0.95)}ms "
        f"max={max(summary.latencies or [0])}ms"
    )
    print(f"报告：{report_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Day 7 路由基线评测（三级路由 + 决策日志口径）")
    parser.add_argument("--fixtures", default="eval/fixtures/day7_intents.jsonl")
    parser.add_argument("--report", default="docs/plans/day07/a3-路由基线.md")
    parser.add_argument(
        "--dump-json",
        default="",
        help="逐条结果落盘（分析 bad case 分布用；建议 eval/artifacts/…，该目录不入库）",
    )
    parser.add_argument(
        "--provider",
        choices=("sidecar", "local", "mock"),
        default="sidecar",
        help=(
            "sidecar=真 HTTP（7001，受 180ms 内部预算约束）；"
            "local=真权重进程内直调（不经 HTTP，需 torch 环境）；"
            "mock=进程内假件（仅自检脚本，不代表模型能力）"
        ),
    )
    args = parser.parse_args()
    code = asyncio.run(main_async(args))
    for pending in _pending_report:
        if not pending:
            continue
        target, payload = pending
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(f"已写入：{target}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
