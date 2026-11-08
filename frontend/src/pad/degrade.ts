/**
 * C-1 三档超时降级提示状态机（B-2 v3.1 冻结口径 + C-D4 方案 A：同一条字幕原地覆盖升级）。
 * 同一轮对话内，slow 三档（asr_slow/llm_slow/tts_slow）是「占位/等待」级提示，
 * unavailable/timeout 是「失败」终态：失败已显后，同轮迟到的 slow 不得回退覆盖（unavailable > slow）；
 * 同级之间后到覆盖先到（BigSubtitle 单一状态位天然覆盖，不叠两条）。
 * 轮次边界（speechstart / asr_partial）由 RealtimeClient 负责复位。
 */
export type DegradeLevel = "slow" | "failure";

const SLOW_CODES: readonly string[] = ["asr_slow", "llm_slow", "tts_slow"];

export function isSlowCode(code: string): boolean {
  return SLOW_CODES.includes(code);
}

/**
 * 计算本轮降级级别：slow 命中且失败终态在显 → "suppressed"（不渲染，防回退）；
 * 其余 slow → "slow"；失败码 → "failure"（永远渲染，覆盖 slow）。
 */
export function nextDegradeLevel(
  prev: DegradeLevel | null,
  code: string,
): DegradeLevel | "suppressed" {
  if (isSlowCode(code)) {
    return prev === "failure" ? "suppressed" : "slow";
  }
  return "failure";
}
