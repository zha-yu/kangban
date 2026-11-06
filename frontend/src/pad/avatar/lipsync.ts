/**
 * 口型驱动（W1 计划第 5 条 + R-6）：Web Audio AnalyserNode 对 TTS 下行输出算 RMS，
 * 驱动 VRM `aa` 单通道 blendshape；静默 >300ms 平滑归零（闭嘴）；30fps 采样上限。
 * 演示级口径：PRD 无 viseme 要求，音素级口型留待 Day 5。
 *
 * 降级口径（不白屏）：AudioContext / analyser 创建失败、或流不可用 → 保持 level=0，
 * 无口型但不影响播放与其他功能；仅在 srcObject 引用变化时重建（重连/重开麦），
 * 失败不会在帧循环里反复重试。
 * 注意：analyser 只接 source 不接 destination，避免口型采集把 TTS 音频再播一遍造成回声。
 */

/** 采样节流：30fps 上限（W1 计划第 5 条），按时间戳判定而非每帧都算 */
const SAMPLE_INTERVAL_MS = 1000 / 30;
/** 静默判定：距最后一次有声样本超过该值 → 目标开合度归零（闭嘴） */
const SILENCE_MS = 300;
/** RMS → 开合度增益（TTS 常见 RMS 0.05~0.3，演示级线性放大后钳到 1） */
const RMS_GAIN = 8;
/** 底噪阈值：低于视为无声，不刷新「最后有声」时刻 */
const RMS_FLOOR = 0.001;
/** 上升平滑系数（每采样步）：开口快、闭口慢，观感更自然 */
const SMOOTH_ATTACK = 0.55;
const SMOOTH_RELEASE = 0.18;
/** FFT size；时间域样本数为 FFT_SIZE/2，512 对口型足够 */
const FFT_SIZE = 512;

/** 每帧取最新 TTS 播放元素（App 接线由主线负责，元素可能在首帧后才出现） */
export type GetAudioEl = () => HTMLAudioElement | null;

export class LipsyncDriver {
  private ctx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  /** 当前绑定的下行流（引用比较，变化即重建 analyser） */
  private boundStream: MediaStream | null = null;
  private timeBuf = new Float32Array(FFT_SIZE / 2);
  /** 平滑后的口型开合度（0~1） */
  private level = 0;
  private lastSoundAt = 0;
  private lastSampleAt = 0;

  constructor(private getAudioEl: GetAudioEl) {}

  /**
   * 每帧调用（rAF 循环内），返回平滑后的开合度（0~1）。
   * analyser 未就绪/创建失败时返回当前 level（通常 0），调用方照常写 aa，静默降级。
   */
  tick(now: number): number {
    // DOM 类型里 srcObject 是 MediaProvider（可能是 Blob），口型只认 MediaStream，其余静默跳过
    const src = this.getAudioEl()?.srcObject ?? null;
    const stream = src instanceof MediaStream ? src : null;
    if (stream !== this.boundStream) this.rebind(stream);
    if (!this.analyser) return this.level;

    // 30fps 上限：未到采样点沿用上次 level（渲染值每帧仍稳定写出）
    if (now - this.lastSampleAt < SAMPLE_INTERVAL_MS) return this.level;
    this.lastSampleAt = now;

    this.analyser.getFloatTimeDomainData(this.timeBuf);
    let sum = 0;
    for (let i = 0; i < this.timeBuf.length; i++) sum += this.timeBuf[i] * this.timeBuf[i];
    const rms = Math.sqrt(sum / this.timeBuf.length);
    if (rms > RMS_FLOOR) this.lastSoundAt = now;

    const target =
      now - this.lastSoundAt > SILENCE_MS ? 0 : Math.min(1, rms * RMS_GAIN);
    const k = target > this.level ? SMOOTH_ATTACK : SMOOTH_RELEASE;
    this.level += (target - this.level) * k;
    return this.level;
  }

  /** srcObject 变化（重连/重开麦）→ 拆旧节点重建；同一 AudioContext 复用 */
  private rebind(stream: MediaStream | null): void {
    this.boundStream = stream;
    this.teardownNodes();
    if (!stream) return;
    try {
      if (!this.ctx) {
        // 首次检测到下行流才创建；TTS 播放有用户手势前置（既有链路保证），
        // sticky activation 下通常直接 running，suspended 时防御性 resume
        this.ctx = new AudioContext();
      }
      if (this.ctx.state === "suspended") void this.ctx.resume().catch(() => {});
      const source = this.ctx.createMediaStreamSource(stream);
      const analyser = this.ctx.createAnalyser();
      analyser.fftSize = FFT_SIZE;
      source.connect(analyser); // 不连 destination：避免回声
      this.source = source;
      this.analyser = analyser;
    } catch {
      // 创建失败 → 静默降级（无口型）；下次流变化才会再尝试，不在帧循环里重试
      this.source = null;
      this.analyser = null;
    }
  }

  private teardownNodes(): void {
    try {
      this.source?.disconnect();
    } catch {
      /* 已断开 */
    }
    try {
      this.analyser?.disconnect();
    } catch {
      /* 已断开 */
    }
    this.source = null;
    this.analyser = null;
  }

  /** 组件卸载：断开节点并关闭 AudioContext（释放系统音频资源） */
  dispose(): void {
    this.teardownNodes();
    this.boundStream = null;
    void this.ctx?.close().catch(() => {});
    this.ctx = null;
  }
}
