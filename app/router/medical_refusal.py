"""医疗拒答固定话术与拦截（C3 · Day 7 · FR-DLG-09）。

**为什么独立一个模块**：
- safety.py 只管「急症一票否决」（红色）——医疗诊断/药理问题不属于红色急症（头疼/头晕
  不进胸部部位词表），但也不能让 LLM 自由编答；
- 拒答是**早于 Agent 的拦截**（和 safety_check 同级），放在 router 层或 safety.py 里
  都会让 safety.py 的职责变成「什么都拦」——独立出来，调用方知道「先过 safety_check、
  再过 medical_refusal_check」。

**三类拒答触发**（命中任一即短路返回固定话术，不进 Agent）：
1. **诊断类**：问自己「是不是高血压/糖尿病/冠心病」、问「得了什么病/确诊/诊断」
2. **药理类**：问「这药治什么/有什么用/有副作用/安全吗」
3. **通用医疗建议**：问「该吃什么药/该怎么办/需要动手术吗」

**话术是写死的固定字符串**（PRD FR-DLG-09），**不生成、不拼接**：
- 不说具体疾病名（高血压/糖尿病/冠心病等）
- 不说「建议看医生」（避免被模型篡改话术加诊断）
- 话术自带「帮您转告医生和护工」（后续由拦截调用方接线 work_orders 转告任务）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)

#: PRD FR-DLG-09 固定拒答话术。**一行字、无拼接、不进 prompt**。
#: 「这个我不能判断」→ 明确边界；「帮您转告医生和护工」→ 告诉长者有后续动作；
#: 「一会儿就来帮您看看」→ 安抚预期，避免长者觉得被踢皮球。
MEDICAL_REFUSAL_TEXT: Final[str] = "这个我不能判断，我帮您转告医生和护工，一会儿就来帮您看看。"

#: 诊断类关键词（命中任一即拒答）。
#: 全是**具体诊断名词或诊断动作**，没有模糊问法（模糊问法归 Agent 自由回答）。
_DIAGNOSIS_KEYWORDS: Final[tuple[str, ...]] = (
    # 疾病名（常见病 + 长者高发）
    "高血压",
    "糖尿病",
    "冠心病",
    "心脏病",
    "中风",
    "脑梗",
    "脑出血",
    "脑溢血",
    "心肌梗塞",
    "心梗",
    "心绞痛",
    "心衰",
    "气管炎",
    "哮喘",
    "慢阻肺",
    "肺气肿",
    "肺炎",
    "肺结核",
    "肝炎",
    "肝硬化",
    "肾结石",
    "胆结石",
    "糖尿病足",
    "白内障",
    # 诊断动作
    "得了什么病",
    "得什么病",
    "什么病",
    "确诊",
    "诊断",
    "诊断一下",
    "帮我看看是什么病",
)

#: 药理类关键词（命中任一即拒答）。
#: 只收「直接问药理」的问法，不收「我吃了这药觉得 xxx」（后者是体验反馈，Agent 可以安慰回应）。
_PHARMACOLOGY_KEYWORDS: Final[tuple[str, ...]] = (
    # 口语化问法（长者日常会说 "这个药"，不是严格书面 "这药"）
    "这个药治什么",
    "这个药是干什么的",
    "这个药有什么用",
    "这个药有什么作用",
    "这个药有副作用吗",
    "这个药有没有副作用",
    "这个药安全吗",
    "这个药好吗",
    "这个药能吃吗",
    "这个药能不能吃",
    "这个药管用吗",
    "这个药对不对",
    "这个药是不是该吃",
    "这个药怎么样",
    # 省略 "个" 的书面/简化写法
    "这药治什么",
    "这药是干什么的",
    "这药有什么用",
    "这药有副作用吗",
    "这药有没有副作用",
    "这药安全吗",
    "这药能吃吗",
)

#: 通用医疗建议关键词（命中任一即拒答）。
#: 「该吃什么药」→ 不能替人开药；「该怎么办/动手术」→ 超出非医疗边界。
_GENERAL_ADVICE_KEYWORDS: Final[tuple[str, ...]] = (
    "我该吃什么药",
    "我该怎么办",
    "我应该怎么办",
    "该怎么办",
    "需要动手术吗",
    "要不要动手术",
    "用不用动手术",
    "要开刀吗",
    "要不要开刀",
    "用不用开刀",
)


@dataclass(frozen=True, slots=True)
class RefusalDecision:
    """医疗拒答判定结果（与 SafetyDecision 同形，调用方可以统一处理短路）。"""

    is_refused: bool
    reason: str | None  # "diagnosis" / "pharmacology" / "general_advice" / None
    matched_keyword: str | None
    message: str | None  # MEDICAL_REFUSAL_TEXT 或 None


def medical_refusal_check(text: str) -> RefusalDecision:
    """医疗拒答拦截（纯函数、无 I/O、毫秒级）。

    调用时机：**safety_check 之后、Agent 之前**。
    - safety_check 判红 → 已短路进安全升级，本函数不被调用
    - safety_check 未判红 → 本函数检查是否命中医疗拒答边界
      - 命中 → 返回固定拒答话术 + 标记 followup_required
      - 未命中 → 继续进 Agent
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return RefusalDecision(is_refused=False, reason=None, matched_keyword=None, message=None)

    for kw in _DIAGNOSIS_KEYWORDS:
        if kw in cleaned:
            logger.info("medical_refusal_hit reason=diagnosis keyword=%r text=%r", kw, cleaned[:40])
            return RefusalDecision(
                is_refused=True,
                reason="diagnosis",
                matched_keyword=kw,
                message=MEDICAL_REFUSAL_TEXT,
            )

    for kw in _PHARMACOLOGY_KEYWORDS:
        if kw in cleaned:
            logger.info("medical_refusal_hit reason=pharmacology keyword=%r text=%r", kw, cleaned[:40])
            return RefusalDecision(
                is_refused=True,
                reason="pharmacology",
                matched_keyword=kw,
                message=MEDICAL_REFUSAL_TEXT,
            )

    for kw in _GENERAL_ADVICE_KEYWORDS:
        if kw in cleaned:
            logger.info("medical_refusal_hit reason=general_advice keyword=%r text=%r", kw, cleaned[:40])
            return RefusalDecision(
                is_refused=True,
                reason="general_advice",
                matched_keyword=kw,
                message=MEDICAL_REFUSAL_TEXT,
            )

    return RefusalDecision(is_refused=False, reason=None, matched_keyword=None, message=None)


#: 全量拒答关键词（调试/测试用，不要在运行时遍历）
ALL_REFUSAL_KEYWORDS: Final[tuple[str, ...]] = (
    *_DIAGNOSIS_KEYWORDS,
    *_PHARMACOLOGY_KEYWORDS,
    *_GENERAL_ADVICE_KEYWORDS,
)


__all__ = [
    "ALL_REFUSAL_KEYWORDS",
    "MEDICAL_REFUSAL_TEXT",
    "RefusalDecision",
    "medical_refusal_check",
]
