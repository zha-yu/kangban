/**
 * W3 单段声纹采集封装（C4）：开麦攒 16k PCM16 帧（复用 audio/capture.ts 的 MicCapture
 * + resample-worklet），组 WAV（audio/wav.ts 的 assembleWav，16k/16bit/单声道）。
 * 段内实时计时 1–5s 有效（任务口径）：不足 1s 禁上传，到 5s 自动截断。
 * 音频只存内存（AC-36：原始音频不落盘、不进 localStorage/sessionStorage），
 * clear()/close() 后帧数组与 Blob 引用即弃。
 */
import { MicCapture } from "../audio/capture";
import { assembleWav } from "../audio/wav";

export const SEGMENT_MIN_MS = 1_000; // 段内有效下限：不足禁上传并提示
export const SEGMENT_MAX_MS = 5_000; // 自动截断上限：到点自动停止

export class SegmentRecorder {
  private mic: MicCapture | null = null;
  private frames: Int16Array[] = [];
  private startedAtMs = 0;
  private autoTimer: ReturnType<typeof setTimeout> | null = null;
  /** begin/end 竞态守卫（沿 HoldToTalk 惯例）：start 期间被 stop 作废则不接管采集 */
  private seq = 0;
  private recording = false;
  private lastDurationMs = 0;

  /** 到 5s 自动截断时回调（组件据此走停止 + 上传流程） */
  onAutoStop: (() => void) | null = null;

  get isRecording(): boolean {
    return this.recording;
  }

  /** 段内实时计时：录音中返回距开始毫秒数，未录返回 0 */
  get elapsedMs(): number {
    return this.recording ? Date.now() - this.startedAtMs : 0;
  }

  /** 开始录音：开麦（已开则复用实例，避免重复弹权限）→ 攒帧 → 挂 5s 定时器。
   *  麦克风失败抛 MicCaptureError（话术已映射，调用方直接展示）。 */
  async start(): Promise<void> {
    const seq = ++this.seq;
    this.frames = []; // 新一次尝试：上一段的残留帧先清掉（AC-36）
    this.lastDurationMs = 0;
    if (!this.mic) this.mic = new MicCapture();
    const mic = this.mic;
    if (!mic.stream) await mic.start();
    if (seq !== this.seq) {
      // start 期间被 stop() 作废（快速连点）：释放本次启动的采集，避免悬挂
      await mic.stop();
      return;
    }
    mic.onFrame = (pcm) => this.frames.push(pcm);
    this.recording = true;
    this.startedAtMs = Date.now();
    this.autoTimer = setTimeout(() => {
      if (seq !== this.seq || !this.recording) return;
      this.stop();
      this.onAutoStop?.();
    }, SEGMENT_MAX_MS);
  }

  /** 停止录音：停表、摘帧回调、清定时器；返回时长（毫秒）供 1s 下限判断。
   *  幂等（可重复调，含自动截断后再调一次），重复调用返回同一次的时长。 */
  stop(): number {
    this.seq++; // 作废在途 start 与旧定时器
    if (this.autoTimer !== null) {
      clearTimeout(this.autoTimer);
      this.autoTimer = null;
    }
    if (this.recording) {
      this.lastDurationMs = this.startedAtMs > 0 ? Date.now() - this.startedAtMs : 0;
      this.recording = false;
      this.startedAtMs = 0;
      if (this.mic) this.mic.onFrame = null;
    }
    return this.lastDurationMs;
  }

  /** 组 WAV Blob（16k/16bit/单声道），C4 multipart 的 audio 字段直传 */
  takeWav(): Blob {
    return assembleWav(this.frames);
  }

  /** AC-36：清空帧数组，原始音频数据即弃（上传发起后立即调用） */
  clear(): void {
    this.frames = [];
  }

  /** 彻底关麦并清帧（三段录完 / 组件卸载时调用，麦克风不留后台） */
  async close(): Promise<void> {
    this.stop();
    this.frames = [];
    if (this.mic) {
      await this.mic.stop();
      this.mic = null;
    }
  }
}
