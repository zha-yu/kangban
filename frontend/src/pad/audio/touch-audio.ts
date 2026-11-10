/**
 * Day 7 TD-1/TD-2：触达音频独立播放通道（契约来源：docs/team_tasks/day07/contracts.md §1/§2）。
 *
 * 口径：
 *  - 触达音频走 WS 文本帧 base64 PCM（s16le / 单声道 16k，C1 口径整段），**不进**
 *    WebRTC 音频轨、不进 PlaybackQueue——与回复流互不打断、互不串流；
 *  - played 回执纪律（宁缺勿假）：只在音频**自然播完**（onended 且未被顶掉）时结算；
 *    打断（新触达顶旧 / 会话拆线 stop()）、解码失败、起播失败一律不回；
 *  - 解码失败 / 起播失败只打 client_diag，不阻塞大字幕（字幕走现有 tts_text 路径）。
 */

import type { TouchAudioPayload } from "../protocol";

/** base64 → s16le/mono PCM → AudioBuffer；格式不符 / 解码失败返回 null（降级纯字幕）。 */
export function decodeTouchAudio(
  ctx: BaseAudioContext,
  payload: TouchAudioPayload,
): AudioBuffer | null {
  if (payload.format !== "pcm_s16le" || payload.sample_rate <= 0) return null;
  try {
    const bin = atob(payload.data_b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    if (bytes.byteLength < 2 || bytes.byteLength % 2 !== 0) return null;
    const view = new DataView(bytes.buffer);
    const frames = bytes.byteLength / 2;
    const buffer = ctx.createBuffer(1, frames, payload.sample_rate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < frames; i++) {
      channel[i] = view.getInt16(i * 2, true) / 32768;
    }
    return buffer;
  } catch {
    return null;
  }
}

export interface TouchPlaybackDeps {
  /** 音频自然播完（TD-2：只在此时结算 played） */
  onPlayed: (touchId: string) => void;
  /** 播放失败/解码失败的诊断上行（仅日志，client_diag playback 段） */
  sendDiag: (ok: boolean, error: string) => void;
}

export class TouchPlayback {
  private ctx: AudioContext | null = null;
  private source: AudioBufferSourceNode | null = null;
  private activeTouchId: string | null = null;

  constructor(private deps: TouchPlaybackDeps) {}

  /** 播一条触达音频；返回 false 表示未能起播（调用方据此不回 played）。 */
  play(touchId: string, payload: TouchAudioPayload): boolean {
    this.stop(); // 同一时刻只播一条触达：新触达顶掉旧触达，旧触达不回 played（宁缺勿假）
    if (!this.ctx) {
      this.ctx = new AudioContext();
    }
    const buffer = decodeTouchAudio(this.ctx, payload);
    if (!buffer) {
      this.deps.sendDiag(false, "touch_audio_decode_failed");
      return false;
    }
    const source = this.ctx.createBufferSource();
    source.buffer = buffer;
    source.onended = () => {
      // 只有未被 stop() 主动顶掉的同一条才结算（stop 会先清 activeTouchId / source）
      if (this.activeTouchId === touchId && this.source === source) {
        this.activeTouchId = null;
        this.source = null;
        this.deps.onPlayed(touchId);
      }
    };
    source.connect(this.ctx.destination);
    this.source = source;
    this.activeTouchId = touchId;
    void this.ctx.resume().catch(() => {});
    source.start();
    return true;
  }

  /** 会话结束 / 新触达顶旧：停止当前播放，**不**回 played（宁缺勿假）。 */
  stop(): void {
    this.activeTouchId = null;
    if (this.source) {
      try {
        this.source.onended = null;
        this.source.stop();
        this.source.disconnect();
      } catch {
        /* 已停止的节点重复 stop 会抛，忽略 */
      }
      this.source = null;
    }
  }
}
