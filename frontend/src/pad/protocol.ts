/**
 * Day 3 模块间接口契约的 A4 侧类型定义（来源：docs/team_tasks_day03/contracts.md v1.0）。
 * 使用规则：下游只准引用本文件的契约类型，不得自行定义接口结构；
 * 契约改动必须先改 contracts.md（群通告 + 冻结人任务卡留痕），再同步此处。
 */

/* ===================== C2 · 信令 WS 消息契约（A2 冻结，A4 消费） =====================
 * 传输：WS /ws/signal，文本帧 JSON，每消息一个对象。
 * 语义：连接后 300ms 内必须首发 auth；auth 前的任何其他消息服务端直接 4401 关闭；
 * 45s 无任何帧 → 服务端判死清理。 */

/** WS 关闭码：认证失败/超时（服务端主动关闭时使用） */
export const WS_CLOSE_UNAUTHORIZED = 4401;

/** 信令 WS 路径（dev 经 Vite 代理 /ws → :8000） */
export const SIGNAL_WS_PATH = "/ws/signal";

/* ---- C→S 客户端发送 ---- */

/** 连接后 300ms 内必须首发；token 为设备会话 access_token（E4 签发），不进 URL。
 *  wake_ts（v1.5 可选）：端侧 KWS 命中时刻（epoch 秒）；未命中/呼叫按钮路径不填，
 *  服务端按 WS accept 时刻兜底（source=backend），metrics wakeup 段不缺行 */
export interface AuthMsg {
  type: "auth";
  token: string;
  wake_ts?: number;
}

/** WebRTC offer（客户端发起） */
export interface OfferMsg {
  type: "offer";
  sdp: string;
}

/** ICE 候选，双向 */
export interface IceMsg {
  type: "ice";
  /** ICE candidate JSON 字符串 */
  candidate: string;
}

/** 心跳，20s 周期 */
export interface PingMsg {
  type: "ping";
}

/** TEN-VAD speechstart；preRoll 由 pad 经 WebRTC 轨回放补发（A2 裁决：服务端不回溯轨道） */
export interface AsrStartMsg {
  type: "asr_start";
}

/** TEN-VAD speechend（0.8s 静音） */
export interface AsrEndMsg {
  type: "asr_end";
}

/** client_diag 的 stage 取值（C5 契约：3D / WebRTC / 链路诊断；
 *  v1.5 追加 playback：端侧 TTS 播放耗时上报，服务端代打 metrics，wakeup 不走本路径） */
export type ClientDiagStage = "vrm_load" | "webrtc" | "vad" | "asr" | "playback";

/** C5 追加（v2）：客户端诊断上行，仅日志、服务端不回执；error 必须已脱敏。
 *  duration_ms（v1.5）：stage=playback 时必填（端侧播放耗时 ≥0），其余 stage 不带 */
export interface ClientDiagMsg {
  type: "client_diag";
  stage: ClientDiagStage;
  ok: boolean;
  error: string;
  duration_ms?: number;
}

/** C5 追加：声纹低置信/不可用时服务端主动确认身份（candidates 恒 1 位 = 设备绑定长者） */
export interface IdentityAskMsg {
  type: "identity_ask";
  /** 本次确认的唯一 id；confirm 必须回带 */
  ask_id: string;
  reason: "low_confidence" | "vp_unavailable";
  candidates: { elder_id: string; display_name: string }[];
  /** 恒 30（秒），前端超时展示同步收起 */
  timeout_s: number;
}

/** C5 追加：pad 大按钮点选确认；elder_id 必须等于 candidates[0].elder_id，否则按未确认处理 */
export interface IdentityConfirmMsg {
  type: "identity_confirm";
  ask_id: string;
  elder_id: string;
}

/** C5 追加：服务端告知最终身份模式；generic（超时/无法确认）时 elder_id 为 null */
export interface IdentityResultMsg {
  type: "identity_result";
  source: "voice" | "confirm" | "generic";
  elder_id: string | null;
}

export type ClientSignal =
  | AuthMsg
  | OfferMsg
  | IceMsg
  | PingMsg
  | AsrStartMsg
  | AsrEndMsg
  | ClientDiagMsg
  | IdentityConfirmMsg;

/* ---- S→C 服务端下发 ---- */

export interface AuthOkMsg {
  type: "auth_ok";
  /** 展示用；业务归属一律服务端裁决 */
  elder_id: string | null;
  room_id: string;
}

export interface AuthFailMsg {
  type: "auth_fail";
  code: "timeout" | "invalid" | "expired";
}

export interface AnswerMsg {
  type: "answer";
  sdp: string;
}

/** ICE 候选（C2 契约标注「双向」：服务端也会下发自己的候选） */
export interface ServerIceMsg {
  type: "ice";
  /** ICE candidate JSON 字符串 */
  candidate: string;
}

export interface PongMsg {
  type: "pong";
}

/** ASR 中间结果（字幕预显，业务不依赖） */
export interface AsrPartialMsg {
  type: "asr_partial";
  text: string;
}

/** ASR 终稿（路由层/业务处理的唯一触发事件） */
export interface AsrFinalMsg {
  type: "asr_final";
  text: string;
}

/** 数字人回复文本（大字幕渲染；打断分两层已定案：前端 speechstart 同一刻立即停播，
 *  服务端 A3 收到 asr_start 停写轨清缓冲——前端不等服务端） */
export interface TtsTextMsg {
  type: "tts_text";
  text: string;
}

/** 服务端降级/异常通知；message 为已脱敏的长者可读话术 */
export interface ErrorMsg {
  type: "error";
  code:
    | "asr_unavailable"
    | "llm_timeout"
    | "tool_breaker"
    | "internal"
    | "asr_slow" // Day 5 B-2：ASR 有结果但太慢
    | "llm_slow" // Day 5 B-2：LLM 有结果但太慢
    | "tts_slow"; // Day 5 B-2：TTS 有结果但太慢
  message: string;
}

export type ServerSignal =
  | AuthOkMsg
  | AuthFailMsg
  | AnswerMsg
  | ServerIceMsg
  | PongMsg
  | AsrPartialMsg
  | AsrFinalMsg
  | TtsTextMsg
  | ErrorMsg
  | IdentityAskMsg
  | IdentityResultMsg;

export type ServerSignalType = ServerSignal["type"];

const SERVER_SIGNAL_TYPES: readonly ServerSignalType[] = [
  "auth_ok",
  "auth_fail",
  "answer",
  "ice",
  "pong",
  "asr_partial",
  "asr_final",
  "tts_text",
  "error",
  "identity_ask",
  "identity_result",
];

/**
 * 解析服务端文本帧：非 JSON / type 未知 → 返回 null（调用方记 trace 后丢弃，不白屏）。
 * 仅校验判别字段，字段级完整性由 TS 类型在编译期约束、由对端契约保证。
 */
export function parseServerSignal(raw: string): ServerSignal | null {
  let obj: unknown;
  try {
    obj = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof obj !== "object" || obj === null) return null;
  const t = (obj as { type?: unknown }).type;
  if (typeof t !== "string" || !SERVER_SIGNAL_TYPES.includes(t as ServerSignalType)) {
    return null;
  }
  return obj as ServerSignal;
}

/* ===================== C3 · 降级 REST 契约（A5 冻结，A4 消费） =====================
 * POST /api/audio/utterance，multipart/form-data，Authorization: Bearer <device access_token>。
 * audio: 16kHz/16bit/单声道 WAV（按住说话整段，含 preRoll）；duration_ms 客户端计时、服务端仅入 trace。
 * 错误：401 token 失效 ｜ 413 超长音频（>30s 拒收）｜ 503 ASR 不可用（body 携带降级话术）。
 * 注意：reply 形状依赖 TTS 决议（audio_url vs 直返 PCM），契约会若改此处必须同步。 */

export interface UtteranceReply {
  text: string;
  /** 服务端合成音频的本地可取地址；无 TTS 时为 null */
  audio_url: string | null;
}

export interface UtteranceResponse {
  /** ASR 终稿 */
  text: string;
  reply: UtteranceReply;
}

export type UtteranceErrorCode = "unauthorized" | "payload_too_large" | "asr_unavailable";
