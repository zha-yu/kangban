"""安全一票否决（AC-04 / FR-DLG-01 / FR-DLG-07）——L0 规则层的急症红色判定。

职责边界（contracts.md v1.0 §1 禁区 3 / AGENTS §5）：

1. **只做一票否决**：命中即红色急症，调用方必须短路（不进 System Two / Agent），
   并经 `app/services/safety_service.py` 的 `escalate_safety_event` 同事务落库三表
   （红色事件+工单+护工通知）；本模块不做意图映射、不写库；
2. **不做固定意图映射**（换一段/天气/点播等归三级路由日）；
3. 词表/阈值全部写死在本文件常量里，**禁止写进 prompt**；
4. `safety_check` 是纯函数：无 I/O、无状态、毫秒级（路由预算 ≤200ms，AGENTS §6）；
5. **接线口径**：急症判定先于一切——`handle_user_turn` 入口第一步调用本模块的
   `safety_check`（Day 6 C-8 / S4 接线），命中即 `escalated_to_agent=false`。

判定口径：

- **「部位 + 症状」双命中**（如「胸口」+「疼」），不靠单字——「腰酸背疼」「心里难受」
  这类无胸部位的表达不触发（风险 R13 反误杀）；
- 部位词表只收「胸」：头疼/腰酸背疼按 FR-DLG-09 医疗边界拒答或 Day 9 黄档处理，
  不进红色（PRD 急症清单：胸痛/疑似中风/呼吸困难/跌倒/轻生/严重副作用/红色血压阈值）；
- 四类 FR-DLG-07 边界不判红（各自有护栏，护栏命中即短路返回）：
  否定（「没有胸痛」「不疼了」）/ 历史（「昨天疼现在好了」，带缓解标记判黄归 Day 9）/
  他人主体（「我爸爸胸痛」，不为本人建红色）/ 模糊（「好像胸口疼」，推测判黄归 Day 9）；
- 无需部位搭配的低歧义多字短语（呼吸困难/摔倒/中风先兆/轻生）短语级命中即判红。

宁可错杀不可漏杀（R-5）：护栏只拦「确定不该判红」的表达；
「昨天开始胸痛」这类无缓解标记的历史陈述仍判红，漏黄不漏红。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------- 词表与护栏常量（写死在代码，不进 prompt，AGENTS §5） ----------------

#: 「部位」词表：急症口径只收胸（多字在前，优先命中更具体词）
CHEST_PARTS: tuple[str, ...] = ("胸口", "心口", "前胸", "胸部", "胸")

#: 「症状」词表：与部位双命中才判红，单字不判（R13 反误杀）
CHEST_SYMPTOMS: tuple[str, ...] = ("绞痛", "刺痛", "压得慌", "压着", "发闷", "疼", "痛", "闷")

#: 无需部位搭配的低歧义多字短语：(短语, 急症类别)。全部 ≥3 字，单字不收
CRITICAL_PHRASES: tuple[tuple[str, str], ...] = (
    # 呼吸困难
    ("呼吸困难", "breathing"),
    ("喘不上气", "breathing"),
    ("喘不上来气", "breathing"),
    ("透不过气", "breathing"),
    ("上不来气", "breathing"),
    # 跌倒
    ("摔倒", "fall"),
    ("跌倒", "fall"),
    ("摔了一跤", "fall"),
    ("倒在地上", "fall"),
    ("爬不起来", "fall"),
    # 疑似中风
    ("嘴歪", "stroke"),
    ("口眼歪斜", "stroke"),
    ("说话不清", "stroke"),
    ("口齿不清", "stroke"),
    ("说不清话", "stroke"),
    ("大舌头", "stroke"),
    ("半边没力气", "stroke"),
    ("半边不能动", "stroke"),
    ("手脚突然发麻", "stroke"),
    # 轻生
    ("不想活", "self_harm"),
    ("活不下去", "self_harm"),
    ("轻生", "self_harm"),
    ("自杀", "self_harm"),
    ("想死", "self_harm"),
)

#: 否定标记：症状/短语命中点向左窗口内出现 → 该处不计数（FR-DLG-07 否定）
NEGATION_MARKERS: tuple[str, ...] = ("不", "没", "无", "别", "未", "非", "差点")

#: 模糊/推测标记：部位/短语命中点向左窗口内出现 → 判黄不判红（FR-DLG-07 模糊，黄档归 Day 9）
HEDGE_MARKERS: tuple[str, ...] = ("好像", "似乎", "可能", "恐怕", "也许", "大概", "是不是")

#: 缓解标记：全文任一命中 → 不判红（FR-DLG-07 历史「昨天疼现在好了」；黄档归 Day 9）
RECOVERY_MARKERS: tuple[str, ...] = (
    "现在好了",
    "已经好了",
    "都好了",
    "好多了",
    "没事了",
    "不疼了",
    "不痛了",
    "不闷了",
)

#: 他人主体标记：部位/短语命中点向左窗口内出现 → 不为本人建红色（FR-DLG-07 他人主体）
OTHER_PERSON_MARKERS: tuple[str, ...] = (
    "我爸",
    "我妈",
    "我妈妈",
    "我爸爸",
    "我爷爷",
    "我奶奶",
    "我外公",
    "我外婆",
    "我老伴",
    "我先生",
    "我太太",
    "邻居",
    "隔壁",
    "朋友",
    "别人",
)

#: 症状命中点向左扫描的否定窗口长度（「没有胸痛」：胸距痛隔 1 字，窗口须 ≥2+词长）
_NEGATION_WINDOW = 4
#: 部位/短语命中点向左扫描的推测窗口长度
_HEDGE_WINDOW = 4
#: 部位/短语命中点向左扫描的他人主体窗口长度
_PERSON_WINDOW = 6
#: 症状允许出现在部位起点之后的最大偏移（含部位自身长度），保证「胸口一阵阵地疼」也命中
_SYMPTOM_PROXIMITY = 4

#: 急症类别（SafetyDecision.category 取值）
CATEGORY_CHEST_PAIN = "chest_pain"
CATEGORY_BREATHING = "breathing"
CATEGORY_FALL = "fall"
CATEGORY_STROKE = "stroke"
CATEGORY_SELF_HARM = "self_harm"

#: 本日最小一票否决只有红档（黄档复核归 Day 9 全量规则集）
SEVERITY_RED = "red"


@dataclass(frozen=True)
class SafetyDecision:
    """一票否决判定结果。

    字段说明：
    - `is_sensitive`：True 即红色急症——调用方必须短路（escalated_to_agent=false，
      不进 Agent），并经 `app/services/safety_service.escalate_safety_event` 落库；
      False 则继续走后续路由；
    - `severity`：判定严重度；命中即 `"red"`（本日只有红档，黄档归 Day 9），未命中 None；
    - `category`：命中的急症类别（chest_pain/breathing/fall/stroke/self_harm），
      未命中为 None；
    - `matched_rule`：命中证据（如「部位『胸口』×症状『疼』」或命中的短语原文），
      落库时进 work_orders/notifications 供护工研判；护栏命中时也带命中词，未命中 None；
    - `reason`：未判红时的护栏依据（negation/recovery/hedge/other_person），
      用于 bad case 回流定位；判红时为 None。
    """

    is_sensitive: bool
    severity: str | None
    category: str | None
    matched_rule: str | None
    reason: str | None


def _not_sensitive(reason: str | None, matched_rule: str | None = None) -> SafetyDecision:
    return SafetyDecision(
        is_sensitive=False,
        severity=None,
        category=None,
        matched_rule=matched_rule,
        reason=reason,
    )


def _sensitive(category: str, matched_rule: str) -> SafetyDecision:
    return SafetyDecision(
        is_sensitive=True,
        severity=SEVERITY_RED,
        category=category,
        matched_rule=matched_rule,
        reason=None,
    )


def _occurrences(haystack: str, needle: str) -> list[int]:
    """子串全部出现位置；空串防误配（「胸」之外的单字词表不存在，双保险）。"""
    if not needle:
        return []
    starts: list[int] = []
    idx = haystack.find(needle)
    while idx != -1:
        starts.append(idx)
        idx = haystack.find(needle, idx + 1)
    return starts


def _window_has_marker(text: str, start: int, markers: tuple[str, ...], window: int) -> bool:
    """命中点左侧 window 个字符内是否出现任一标记。"""
    left = text[max(0, start - window) : start]
    return any(marker in left for marker in markers)


def safety_check(text: str) -> SafetyDecision:
    """急症一票否决判定：纯函数、无 I/O、毫秒级。

    顺序：全文缓解护栏 → 胸部「部位×症状」双命中 → 低歧义短语命中。
    任何护栏命中即不判红（宁可漏黄不漏红由护栏白名单保证，见模块 docstring）。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return _not_sensitive(None)

    # 1. 缓解护栏（全文）：带「已缓解」语义的历史表达不判红（FR-DLG-07 历史）
    for marker in RECOVERY_MARKERS:
        if marker in cleaned:
            return _not_sensitive("recovery", matched_rule=marker)

    # 2. 胸部「部位 × 症状」双命中
    blocked_reason: str | None = None
    for part in CHEST_PARTS:
        for part_start in _occurrences(cleaned, part):
            # 他人主体护栏：部位左侧窗口出现他人标记 → 不为本人建红色（FR-DLG-07）
            if _window_has_marker(cleaned, part_start, OTHER_PERSON_MARKERS, _PERSON_WINDOW):
                return _not_sensitive("other_person", matched_rule=part)
            # 模糊护栏：部位左侧窗口出现推测标记 → 判黄不判红（FR-DLG-07 模糊）
            if _window_has_marker(cleaned, part_start, HEDGE_MARKERS, _HEDGE_WINDOW):
                return _not_sensitive("hedge", matched_rule=part)

            part_end = part_start + len(part)
            for symptom in CHEST_SYMPTOMS:
                for sym_start in _occurrences(cleaned, symptom):
                    # 邻近性：症状必须紧跟部位（防「胸口」与「疼」远距离误配对）
                    if not (part_start <= sym_start <= part_end + _SYMPTOM_PROXIMITY):
                        continue
                    # 否定护栏：症状左侧窗口出现否定标记 → 该处症状不计数（FR-DLG-07 否定）
                    if _window_has_marker(cleaned, sym_start, NEGATION_MARKERS, _NEGATION_WINDOW):
                        blocked_reason = "negation"
                        continue
                    return _sensitive(
                        CATEGORY_CHEST_PAIN, matched_rule=f"部位「{part}」×症状「{symptom}」"
                    )
    if blocked_reason is not None:
        return _not_sensitive(blocked_reason)

    # 3. 低歧义短语命中（呼吸困难/跌倒/中风先兆/轻生，无需部位搭配）
    for phrase, category in CRITICAL_PHRASES:
        for phrase_start in _occurrences(cleaned, phrase):
            # 他人主体护栏（如「我爸摔倒」不为本人建红色）
            if _window_has_marker(cleaned, phrase_start, OTHER_PERSON_MARKERS, _PERSON_WINDOW):
                return _not_sensitive("other_person", matched_rule=phrase)
            # 模糊护栏（如「好像摔倒了」判黄归 Day 9）
            if _window_has_marker(cleaned, phrase_start, HEDGE_MARKERS, _HEDGE_WINDOW):
                return _not_sensitive("hedge", matched_rule=phrase)
            # 否定护栏（如「没有摔倒」「差点摔倒」）
            if _window_has_marker(cleaned, phrase_start, NEGATION_MARKERS, _NEGATION_WINDOW):
                blocked_reason = "negation"
                continue
            return _sensitive(category, matched_rule=phrase)
    if blocked_reason is not None:
        return _not_sensitive(blocked_reason)

    return _not_sensitive(None)


__all__ = [
    "CATEGORY_BREATHING",
    "CATEGORY_CHEST_PAIN",
    "CATEGORY_FALL",
    "CATEGORY_SELF_HARM",
    "CATEGORY_STROKE",
    "CHEST_PARTS",
    "CHEST_SYMPTOMS",
    "CRITICAL_PHRASES",
    "HEDGE_MARKERS",
    "NEGATION_MARKERS",
    "OTHER_PERSON_MARKERS",
    "RECOVERY_MARKERS",
    "SEVERITY_RED",
    "SafetyDecision",
    "safety_check",
]
