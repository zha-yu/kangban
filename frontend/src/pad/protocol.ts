/**
 * Day 3 模块间接口契约的 A4 侧类型定义（来源：docs/team_tasks_day03/contracts.md v1.0）。
 * 使用规则：下游只准引用本文件的契约类型，不得自行定义接口结构；
 * 契约改动必须先改 contracts.md（群通告 + 冻结人任务卡留痕），再同步此处。
 * Day 6 增量来源：docs/team_tasks/day06/contracts.md v1.0 §4（tts_text v2 + segment_ack），
 * 服务端 TypedDict 同步由 dev-C 落 app/audio/signaling.py（写锁 L6）。
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

/** Day 7 新增（day07/contracts.md §2）：触达语音播完回执（C→S）。
 *  只在触达音频**自然播完**后上报；打断 / 播放失败 / 离线不回（宁缺勿假）。
 *  三字段均为服务端 tts_text 触达字段原样回传，客户端不得自造；
 *  与回复流 segment_ack 互不替代：触达消息不进 PlaybackQueue。 */
export interface TouchPlayedMsg {
  type: "touch_played";
  touch_id: string;
  event_id: string;
  attempt_no: number;
}

/** Day 7 新增（day07/README §5.4 草案先行，**待 A1 契约冻结后逐字段校对**）：打断上行（C→S）。
 *  前端 speechstart 本地停播后立即发送，不等回执（A2 裁决：打断不等服务端）；
 *  response_id 取当前活跃回复（PlaybackQueue.activeResponseId），无活跃回复不发送；
 *  旧 epoch 的回调一律丢弃。不加第二套路由消息。 */
export interface InterruptMsg {
  type: "interrupt";
  response_id: string;
  reason: "barge_in";
}

/** Day 7 联调（L6#8）：pad 确认按钮/输入框降级的文本上行。
 *  服务端按 ASR FINAL 同路处理（字幕→编排→用药确认词表判定）；
 *  A1 契约冻结后按 contracts-system-one.md 逐字段校对。 */
export interface UserTextMsg {
  type: "user_text";
  text: string;
}

/** Day 7 联调（L6#9）：主动触达的任务卡片命令（scheduler / 子图下发）。
 *  shape 与 task-channel.ts 的 PadTaskCommand 对齐（snake_case 下行、camelCase snapshot）；
 *  同 taskId 重复下发前端幂等（FR-MED-04），不重启面板。
 *  Day 8（day08/contracts.md §C-1）：kind 扩 "health"（每日健康采集面板触发）。 */
export interface TaskCommandMsg {
  type: "task_command";
  task_id: string;
  kind: "medication" | "health";
  source: "scheduler" | "ws_fallback" | "staff" | "voiceprint";
  hint: string;
  payload:
    | {
        kind: "medication";
        snapshot: {
          drugName: string;
          dose: string;
          scheduledAt: string;
          usage: string;
          attemptNo: number;
          windowSeconds: number;
          snapshotAt: string;
        };
      }
    | {
        kind: "health";
        /** 触达时刻展示标签（HH:MM），前端只展示不参与分级 */
        startedAt: string;
        /** 与后端 health_records.source 前缀口径对齐；仅展示 */
        source: "voice_scheduled" | "staff_scheduled";
      };
  issued_at: number;
}

export type ClientSignal =
  | AuthMsg
  | OfferMsg
  | IceMsg
  | PingMsg
  | AsrStartMsg
  | AsrEndMsg
  | ClientDiagMsg
  | IdentityConfirmMsg
  | SegmentAckMsg
  | TouchPlayedMsg
  | InterruptMsg
  | UserTextMsg;

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
 *  服务端 A3 收到 asr_start 停写轨清缓冲——前端不等服务端）。
 *  Day 6 契约扩展（contracts.md v1.0 §4.1，向后兼容）：新字段全部可选，缺失时
 *  按整段一播处理（旧服务端/回滚路径不报错）；epoch 为客户端原样回传值，前端不得自增；
 *  分段由服务端切分，前端不二次切分；segment_id 与 index 均 0 基且同值。
 *  Day 7 TD-1 扩展（day07/contracts.md §1）：触达四字段全部可选、仅触达消息携带，
 *  回复流消息不出现；audio 缺省 = 纯字幕降级（与 Day 6 形状一致）。 */
/** Day 7 TD-1 触达音频载荷（仅触达消息携带；对话回复流不带） */
export interface TouchAudioPayload {
  format: "pcm_s16le";
  /** 恒 16000（C1 口径） */
  sample_rate: number;
  /** s16le / 单声道 PCM 整段的 base64 */
  data_b64: string;
}

export interface TtsTextMsg {
  type: "tts_text";
  text: string;
  degraded?: boolean;
  /** 路由降级标记（D6，adhoc-20261111-route_degraded 契约冻结 v1.0）：
   *  L0/L1 不可用经 fallback 由 L2 Agent 正常生成时为 true；与 degraded 正交。
   *  服务端每条恒下发 true/false；前端不做缺省推断。 */
  route_degraded?: boolean;
  /** 本次回复唯一 id（多段共用）；缺失 → 整段一播 */
  response_id?: string;
  /** 会话代次，客户端原样回传，不得自增 */
  epoch?: number;
  /** 0 基段号 */
  segment_id?: number;
  /** 0 基序号，与 segment_id 同值（保留两字段便于未来乱序） */
  index?: number;
  /** 本回复总段数 */
  total?: number;
  /** Day 7 TD-1 触达：触达唯一 message_id，touch_played 回带 */
  touch_id?: string;
  /** Day 7 TD-1 触达：用药事件 uuid 字符串，回传用 */
  touch_event_id?: string;
  /** Day 7 TD-1 触达：第几次触达（1=T0 … 4=T+15），回传用 */
  touch_attempt_no?: number;
  /** Day 7 TD-1 触达：合成成功才携带；缺省 = 纯字幕降级 */
  audio?: TouchAudioPayload;
}

/** Day 6 新增（contracts.md v1.2 §4.2）：pad 分段送达确认上报（C→S）。
 *  status=playing 该段起播 / played 该段播完（唯一推进服务端水位 last_acked_segment 的信号，
 *  初始 -1）/ canceled 打断——segment_id 填最后已确认 played 的段号，**无一完整播放段时
 *  填 -1 哨兵**（v1.2 裁决，与服务端水位初始值同构；canceled 类 ack 只走服务端校验①②③，
 *  跳过单调校验④）；
 *  at_ms 为客户端单调时钟（ms），只用于折算播放时长，不参与判定；
 *  上报走「尽力而为 + 1 次重发」，失败只打 client_diag，不阻塞播放。 */
export interface SegmentAckMsg {
  type: "segment_ack";
  response_id: string;
  epoch: number;
  segment_id: number;
  status: "playing" | "played" | "canceled";
  at_ms: number;
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
  | IdentityResultMsg
  | TaskCommandMsg
  | MediaPlayMsg
  | MediaControlMsg;

export type ServerSignalType = ServerSignal["type"];

/** D4（X8 口径②，2026-11-11 主理人给定帧形状）：play_opera 命中后 companion 经
 *  signaling.deliver 下发；pad 直连 media_url（§6.1 稳定路径，wav|mp3 不感知）。 */
export interface MediaPlayMsg {
  type: "media_play";
  media_url: string;
  title: string;
  media_id?: string;
  duration?: number | null;
}

/** D4（X8 口径③）：播放控制七动作（契约 §6.3 表）；next_track 由服务端取好下一段
 *  随帧携带新 media（「换源」），pad 不猜下一段。 */
export interface MediaControlMsg {
  type: "media_control";
  action:
    | "pause"
    | "resume"
    | "stop"
    | "next_track"
    | "volume_up"
    | "volume_down"
    | "replay";
  media_url?: string;
  title?: string;
  media_id?: string;
  duration?: number | null;
}

const SERVER_SIGNAL_TYPES: readonly ServerSignalType[] = [
  "auth_ok",
  "auth_fail",
  "task_command",
  "answer",
  "ice",
  "pong",
  "asr_partial",
  "asr_final",
  "tts_text",
  "error",
  "identity_ask",
  "identity_result",
  "media_play",
  "media_control",
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
