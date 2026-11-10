"""L1 规则层（Day 7 · A3）：高频、无歧义、零成本的固定指令。

**为什么要有规则层**：PRD FR-CON-03 的播放控制指令（暂停/继续/停止/换一段/音量/重复）
是长者最常说、也最不该等分类器的几句话。走规则层 = 0 次模型调用、0 次 Agent 调用，
延迟是「几微秒」而不是「200ms」。

三条纪律（违反即返工）：

1. **纯函数**：无 I/O、无 DB、不读 `settings`、不 import 任何 app 内其它业务模块。
   本文件只 import `re` / `dataclasses` / `typing` 与 `app.system_one.schemas.Intent`；
   出现 HTTP 客户端、数据库会话或 ORM 模型即视为违规——`tests/test_router_rules.py`
   用 AST 断言 import 图，不靠肉眼 grep。
2. **≤10 条**：规则层膨胀就变成第二个意图分类器，且没人再 review 它。超出部分交
   System One / Agent（PRD 附录 A 的 10 条：播放控制 8 + 天气 1 + 用药查询 1）。
3. **否定句必须不误命中**：`不用停` / `别暂停` 这类句子由 `_negated()` 挡住，
   测试里逐条钉死（「宁可漏识别，不可误执行」——规则层命中会直接产生动作）。

来源：PRD FR-CON-03 播放控制指令集 + FR-CON-01 天气 + FR-DLG-02 固定意图。
**不是**大纲课时的十句清单（AGENTS 前置铁律：大纲不提供事实依据）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.system_one.schemas import Intent

#: 规则表版本。改任一规则（含正则、顺序、动作）都要 +1：
#: `decision_logs.model_version` 会带上它，否则「换了一版规则后准确率变了」无法追溯。
RULESET_VERSION: Final[str] = "r1"

#: 规则命中后的动作：`tool` = 直连工具（只经 ToolExecutor）；`agent` = 下沉主控。
#: 注意：`action="agent"` 且 `route_layer="rule"` 是**允许**的组合——规则认出了意图，
#: 但该能力（媒体库/用药查询）本日未就绪，只能交给 Agent 用话术兜住（计划 §10-2 裁决）。
RuleAction = Literal["tool", "agent"]

#: 能力未就绪的统一 reason（写进 `decision_logs` 之外的日志字段，便于统计）
REASON_CAPABILITY_NOT_READY: Final[str] = "capability_not_ready"
#: 规则命中但槽位缺失（如问天气却没说城市）
REASON_MISSING_SLOT: Final[str] = "missing_slot"

#: 否定标记：命中点左侧窗口内出现即视为否定（不是「指令」）
_NEGATION_MARKERS: Final[tuple[str, ...]] = ("不用", "不要", "别", "无需", "不必", "先不", "不想")
#: 否定窗口长度（字符）：`不用暂停` = 2 字前缀，留 3 字余量
_NEGATION_WINDOW: Final[int] = 3

#: 城市词表（天气工具唯一必填槽位）。只收高频城市名，**不做地理编码**：
#: 抽不到城市就不直连（转 Agent 追问），绝不猜一个默认城市糊弄长者。
CITY_NAMES: Final[tuple[str, ...]] = (
    "北京",
    "上海",
    "天津",
    "重庆",
    "广州",
    "深圳",
    "杭州",
    "南京",
    "苏州",
    "无锡",
    "宁波",
    "温州",
    "合肥",
    "福州",
    "厦门",
    "南昌",
    "济南",
    "青岛",
    "烟台",
    "郑州",
    "洛阳",
    "武汉",
    "长沙",
    "成都",
    "贵阳",
    "昆明",
    "南宁",
    "桂林",
    "海口",
    "三亚",
    "西安",
    "兰州",
    "西宁",
    "银川",
    "太原",
    "石家庄",
    "沈阳",
    "大连",
    "长春",
    "哈尔滨",
    "呼和浩特",
    "乌鲁木齐",
    "拉萨",
)

_CITY_PATTERN: Final[re.Pattern[str]] = re.compile("|".join(CITY_NAMES))


@dataclass(frozen=True, slots=True)
class RuleHit:
    """一次规则命中。`confidence=1.0` 是**口径声明**，不是模型输出：规则命中即确定。"""

    rule_id: str
    intent: Intent
    action: RuleAction
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    confidence: float = 1.0
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Rule:
    """一条规则。`patterns` 任一命中即算命中；`negation_safe=False` 的规则自带否定词形。"""

    rule_id: str
    intent: Intent
    action: RuleAction
    patterns: tuple[str, ...]
    tool_name: str | None = None
    reason: str | None = None
    #: True = 命中点左侧出现否定标记时**不算命中**（默认安全）；少数规则（如「别放了」）
    #: 本身就是指令，把它们写成完整短语即可自然绕开否定窗口。
    negation_safe: bool = True
    #: 反向排除词：全文出现任一即**不算命中**。用途是把「戏曲控制」与「电影控制」分开——
    #: 「昨天那个电影还没看完，接着放」会撞 R-02 的「接着放」，但它不是戏曲动作。
    excludes: tuple[str, ...] = ()


#: 规则表（顺序即优先级）。**≤10 条**，逐条有正例/负例（见 tests/test_router_rules.py）。
RULESET: Final[tuple[Rule, ...]] = (
    # R-04 打断播报：先说，因为它与「停止播放」语义最接近但对象不同（回复流 vs 媒体）
    Rule(
        rule_id="R-04",
        intent="chat",
        action="agent",
        patterns=(r"别说了", r"你先别说话", r"打住", r"打断一下"),
        reason=REASON_CAPABILITY_NOT_READY,
        negation_safe=False,  # 「别说了」本身就是指令，不能被自己的「别」判成否定
    ),
    # R-01 暂停播放
    Rule(
        rule_id="R-01",
        intent="opera",
        action="agent",
        patterns=(r"暂停", r"停一下", r"先停停", r"歇一会(儿)?"),
        reason=REASON_CAPABILITY_NOT_READY,
    ),
    # R-02 继续播放
    Rule(
        rule_id="R-02",
        intent="opera",
        action="agent",
        patterns=(r"继续(播放|放|唱)?", r"接着(放|播|唱|说)", r"往下放", r"接着来吧"),
        reason=REASON_CAPABILITY_NOT_READY,
        excludes=("电影", "影片", "片子"),
    ),
    # R-03 停止播放
    Rule(
        rule_id="R-03",
        intent="opera",
        action="agent",
        patterns=(r"停止(播放|放映)?", r"关掉(戏曲|收音机|电视|音乐|声音)?", r"别放了", r"不听了"),
        reason=REASON_CAPABILITY_NOT_READY,
        negation_safe=False,  # 「别放了」是停止指令
    ),
    # R-05 换一段
    Rule(
        rule_id="R-05",
        intent="opera",
        action="agent",
        patterns=(r"换(一|个)?段", r"换一(个|首)", r"换(台|曲)", r"再来一段"),
        reason=REASON_CAPABILITY_NOT_READY,
        excludes=("电影", "影片", "片子"),
    ),
    # R-06 调大音量（放在 R-07 前：「大声点」不该被 R-07 的「声」抢走）
    Rule(
        rule_id="R-06",
        intent="opera",
        action="agent",
        patterns=(r"大(一)?点声", r"声音(调)?大", r"音量(调)?大", r"大声(一)?点", r"听不清"),
        reason=REASON_CAPABILITY_NOT_READY,
    ),
    # R-07 调小音量
    Rule(
        rule_id="R-07",
        intent="opera",
        action="agent",
        patterns=(r"小(一)?点声", r"声音(调)?小", r"音量(调)?小", r"小声(一)?点", r"太吵"),
        reason=REASON_CAPABILITY_NOT_READY,
    ),
    # R-08 重复一遍
    Rule(
        rule_id="R-08",
        intent="opera",
        action="agent",
        patterns=(r"再(放|说|唱|播)(一)?遍", r"重复(一遍|播放|一下)"),
        reason=REASON_CAPABILITY_NOT_READY,
        excludes=("电影", "影片", "片子"),
    ),
    # R-09 天气：**唯一真直连**（走 ToolExecutor → MCP weather）
    Rule(
        rule_id="R-09",
        intent="weather",
        action="tool",
        patterns=(
            r"(天气|气温)(怎么样|如何|咋样|好吗|预报)",
            r"(查一下|看看|问问|报一下).{0,4}(天气|气温)",
            r"(今天|明天|后天|外面|现在).{0,6}(天气|气温)",
            r"要(不要|用不用)?带伞",
            r"(冷不冷|热不热|下不下雨|会不会下雨|下雨吗|下雨了没)",
        ),
        tool_name="get_weather",
    ),
    # R-10 用药查询/确认入口（C1 工具需从对话解析 answer，本日下沉 Agent）
    Rule(
        rule_id="R-10",
        intent="medication",
        action="agent",
        patterns=(
            r"药(还)?剩(多少|几)",
            r"还剩几(片|粒)",
            r"吃什么药",
            r"我的药",
            r"用药(安排|计划|清单)",
            r"该吃药了吗",
        ),
        reason=REASON_CAPABILITY_NOT_READY,
    ),
)

#: 预编译（模块加载时一次；`re` 缓存未命中会每次都做正则编译）
_COMPILED: Final[tuple[tuple[Rule, tuple[re.Pattern[str], ...]], ...]] = tuple(
    (rule, tuple(re.compile(p) for p in rule.patterns)) for rule in RULESET
)


def _negated(text: str, start: int) -> bool:
    """命中点左侧 `_NEGATION_WINDOW` 个字符内是否有否定标记（「不用停」「别暂停」）。"""
    left = text[max(0, start - _NEGATION_WINDOW) : start]
    return any(marker in left for marker in _NEGATION_MARKERS)


def extract_city(text: str) -> str | None:
    """从文本里取城市名（第一个命中）。抽不到返回 None——**不猜默认城市**。"""
    match = _CITY_PATTERN.search(text or "")
    return match.group(0) if match else None


def match_rule(text: str) -> RuleHit | None:
    """按 `RULESET` 顺序匹配，返回首个命中；无命中返回 None。

    纯函数、无 IO。`text` 为空直接 None（空文本不该触发任何动作）。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None

    for rule, patterns in _COMPILED:
        if rule.excludes and any(word in cleaned for word in rule.excludes):
            continue
        for pattern in patterns:
            for match in pattern.finditer(cleaned):
                if rule.negation_safe and _negated(cleaned, match.start()):
                    continue
                return _hit_for(rule, cleaned)
    return None


def _hit_for(rule: Rule, text: str) -> RuleHit:
    """把规则 + 文本落成 `RuleHit`（天气要额外抽城市槽位）。"""
    if rule.rule_id == "R-09":
        city = extract_city(text)
        if city is None:
            # 认出了天气意图但没有城市：不直连（get_weather 的 city 必填），交 Agent 追问
            return RuleHit(
                rule_id=rule.rule_id,
                intent=rule.intent,
                action="agent",
                reason=REASON_MISSING_SLOT,
            )
        return RuleHit(
            rule_id=rule.rule_id,
            intent=rule.intent,
            action="tool",
            tool_name=rule.tool_name,
            tool_args={"city": city},
        )
    return RuleHit(
        rule_id=rule.rule_id,
        intent=rule.intent,
        action=rule.action,
        tool_name=rule.tool_name,
        reason=rule.reason,
    )


__all__ = [
    "CITY_NAMES",
    "REASON_CAPABILITY_NOT_READY",
    "REASON_MISSING_SLOT",
    "RULESET",
    "RULESET_VERSION",
    "Rule",
    "RuleAction",
    "RuleHit",
    "extract_city",
    "match_rule",
]
