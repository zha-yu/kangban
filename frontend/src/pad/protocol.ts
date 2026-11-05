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

/** 连接后 300ms 内必须首发；token 为设备会话 access_token（E4 签发），不进 URL */
export interface AuthMsg {
  type: "auth";
  token: string;
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

export type ClientSignal =
  | AuthMsg
  | OfferMsg
  | IceMsg
  | PingMsg
  | AsrStartMsg
  | AsrEndMsg;

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
  code: "asr_unavailable" | "llm_timeout" | "tool_breaker" | "internal";
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
  | ErrorMsg;

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
