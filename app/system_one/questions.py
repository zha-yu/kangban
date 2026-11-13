"""System One 的题面（questions）与 state 构造：**唯一入口**（Day 7 · A1）。

为什么单独一个文件、且必须唯一：
- 题面是**产品决策**（哪些意图、边界怎么划、敏感命题怎么写），散落在路由代码里就没人能 Review；
- `decision_logs` 的评测口径依赖题面稳定（换一版题面，历史准确率就不可比）；
- 模型对**选项顺序敏感**（spike README §4），候选顺序必须写死在常量里，不许运行时乱序。

三条来自实测的硬约束（`spikes/chinese_jev_hello/README.md`）：
1. **state 不能只塞 ASR 文本**：20 条里 2 个错例（"换一段，这段不好听""声音小一点，太吵了"）
   都因为不知道"当前正在放戏曲"。所以统一走 `build_state()` 带 `current_task` / `media_status`。
2. **`head_max_len=256`**：7 个候选的描述必须短，否则每个选项被截到 3~4 token，准确率断崖。
3. **快路径只问 intent**：CPU 实测单问 p95 167ms（达标），两问 285ms、四问 668ms（超标）。
   故拆成 `build_fast_questions()`（路由用）与 `build_shadow_questions()`（安全信号，不阻塞路由）。

本模块是纯函数：不读 settings、不碰 DB、不 import 任何 app 内其它模块。
"""

from __future__ import annotations

import json
from typing import Any, Final

from app.system_one.schemas import Intent

#: 题面版本：改候选/描述/顺序都要 +1，`decision_logs.model_version` 会带上它，用来区分
#: q2（2026-11-10）：按 110 条 bad case 修三处划界——闲聊补中文自然说法（说心情/睡眠/往事）、
#: 天气与「问日期星期」划界、电影（影片/喜剧片）与戏曲（京剧越剧）划界；其他补设备/视频通话
#: "同权重不同题面"导致的准确率差异。
QUESTIONSET_VERSION: Final[str] = "q2"

#: 模型返回的键 → 业务 Intent。用中文键是因为权重是中文/多语言权重（讲义实测口径），
#: 但**业务枚举保持英文**，映射写死在这里，不许下游再猜。
INTENT_BY_KEY: Final[dict[str, Intent]] = {
    "闲聊陪伴": "chat",
    "用药确认": "medication",
    "戏曲点播": "opera",
    "电影点播": "movie",
    "天气查询": "weather",
    "健康采集": "health_checkup",
    "其他": "other",
}

#: 候选顺序即模型看到的顺序（对顺序敏感），**不要重排**。
INTENT_CRITERIA: Final[dict[str, str]] = {
    "闲聊陪伴": "聊天问候、说心情、回忆往事、睡眠倾诉、问常识、找人说说话",
    "用药确认": "回答是否吃过药、问自己的药剩多少、确认用药安排",
    "戏曲点播": "播戏曲（京剧越剧等），或播放中换一段、重复、调音量",
    "电影点播": "看电影、放影片、找片子（喜剧片战争片等）",
    "天气查询": "问室外天气、温度、下雨、穿衣；不含问日期星期",
    "健康采集": "记录或查询血压血糖体温等健康数据",
    "其他": "叫护工、联系家属、问日期星期、翻译英文、拉窗帘开空调等以上都不属于的请求",
}

#: 意图题（快路径唯一的一题）。
INTENT_QUESTION: Final[dict[str, Any]] = {
    "type": "choice",
    "instructions": "根据长者这句话和当前场景，判断它主要属于哪一类意图",
    "criteria": INTENT_CRITERIA,
}

#: 敏感命题（shadow 用）。命题写成"本人 + 需立即处理"，避免把"我爸爸胸痛"算到本人头上
#: （PRD FR-DLG-07 他人主体边界）。
SENSITIVE_QUESTION: Final[dict[str, Any]] = {
    "type": "noul",
    "instructions": "命题：长者本人这句话表达了需要立即处理的健康紧急情况（胸痛、呼吸困难、晕倒、剧烈疼痛、轻生念头）",
    "criteria": {
        "false": "没有表达本人上述紧急症状，可按普通流程处理",
        "true": "表达了本人上述紧急症状之一，需要立即通知护工",
    },
}

#: 隐私收敛（四信号的第四路；双人间场景用）。
PRIVACY_QUESTION: Final[dict[str, Any]] = {
    "type": "choice",
    "instructions": "判断这句话涉及的内容适合在什么范围内讨论",
    "criteria": {
        "personal": "涉及本人健康、用药、身体感受等私密内容",
        "shared": "在场长者都能听的公共话题，如天气、戏曲、活动",
        "none": "没有涉及隐私内容",
    },
}

#: 域外检测（P2 说明项，PRD 对第 4 信号只要求"用于隐私收敛"，域外作为降级判据保留）。
OUT_OF_DOMAIN_QUESTION: Final[dict[str, Any]] = {
    "type": "noul",
    "instructions": "命题：这句话超出了康养陪伴助手能处理的业务范围（如编程、法律诉讼、金融投资、外语翻译）",
    "criteria": {
        "false": "属于康养陪伴助手的业务范围",
        "true": "明显超出业务范围",
    },
}


def build_questions() -> dict[str, dict[str, Any]]:
    """四问全集（契约冻结用；**不在 200ms 快路径上**，四问实测 p95 668ms）。"""
    return {
        "intent": dict(INTENT_QUESTION),
        "is_sensitive": dict(SENSITIVE_QUESTION),
        "privacy_scope": dict(PRIVACY_QUESTION),
        "is_out_of_domain": dict(OUT_OF_DOMAIN_QUESTION),
    }


def build_fast_questions() -> dict[str, dict[str, Any]]:
    """快路径：只问 intent（CPU 实测 p95 167ms，守住 PRD §5.3 的 200ms）。"""
    return {"intent": dict(INTENT_QUESTION)}


def build_shadow_questions() -> dict[str, dict[str, Any]]:
    """Shadow：is_sensitive ＋ privacy_scope 两问并行（Day 10 B3 扩第二问）。

    仍是「并行发出、有果才用，超时/失败就当没问过，不阻塞路由」——shadow 预算
    400ms 独立于快路径 200ms；两问实测 285ms（jev/cpu p95）在预算内。privacy_scope
    的三值语义见 `PRIVACY_QUESTION`，缺答时 `map_answers` 保持默认 "none" 且上层
    以 degraded 标记区分「没问出来」。
    """
    return {
        "is_sensitive": dict(SENSITIVE_QUESTION),
        "privacy_scope": dict(PRIVACY_QUESTION),
    }


def build_state(
    asr_final: str,
    *,
    current_task: str | None = None,
    media_status: str | None = None,
    elder_name: str | None = None,
) -> dict[str, Any]:
    """构造 state。**只放判断真正需要的字段**（噪声会拉低准确率），不塞健康原始数据。

    上下文这两项不是装饰：没有 `media_status` 时"换一段""声音小一点"会被判成闲聊/其他
    （20 条实测的 2 个错例），有了才谈得上准确率。
    """
    state: dict[str, Any] = {"asr_final": asr_final}
    if elder_name:
        state["elder_name"] = elder_name
    if current_task:
        state["current_task"] = current_task
    if media_status:
        state["media_status"] = media_status
    return state


def state_to_text(state: str | dict[str, Any] | list[Any]) -> str:
    """给日志/脱敏用的稳定文本形态（dict/list 走 JSON，ensure_ascii=False 保中文）。"""
    if isinstance(state, str):
        return state
    return json.dumps(state, ensure_ascii=False, sort_keys=True)


def intent_of_key(key: str) -> Intent:
    """模型返回的候选键 → 业务枚举。未知键一律落 `other`（不抛异常，避免打崩一轮对话）。"""
    return INTENT_BY_KEY.get(key, "other")


__all__ = [
    "INTENT_BY_KEY",
    "INTENT_CRITERIA",
    "QUESTIONSET_VERSION",
    "build_fast_questions",
    "build_questions",
    "build_shadow_questions",
    "build_state",
    "intent_of_key",
    "state_to_text",
]
