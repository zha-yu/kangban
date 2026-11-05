/** T-C 麦克风采集封装：getUserMedia（constraints 按 adhoc §2.3）+ AudioWorklet 重采样
 *  + 600ms preRoll 环形缓冲（10 帧 × 60ms，保句首，§4.2）。 */

export const TARGET_RATE = 16000;
export const FRAME_SAMPLES = 960; // 60ms @16k
export const PRE_ROLL_FRAMES = 10; // 600ms preRoll

/** getUserMedia 错误映射（adhoc §2.4，长者可读话术） */
export type MicErrorCode = "not_allowed" | "not_found" | "not_readable";
export const MIC_ERROR_TEXT: Record<MicErrorCode, string> = {
  not_allowed: "麦克风权限被拒绝：请点击地址栏图标允许后重试",
  not_found: "未检测到麦克风设备",
  not_readable: "麦克风被其他程序占用",
};

export class MicCaptureError extends Error {
  constructor(
    public code: MicErrorCode,
    message: string,
  ) {
    super(message);
  }
}

function mapGetUserMediaError(err: unknown): MicCaptureError {
  const name = err instanceof DOMException ? err.name : "";
  const code: MicErrorCode =
    name === "NotFoundError" || name === "OverconstrainedError"
      ? "not_found"
      : name === "NotReadableError" || name === "AbortError"
        ? "not_readable"
        : "not_allowed";
  return new MicCaptureError(code, MIC_ERROR_TEXT[code]);
}

/** constraints（adhoc §2.3）：AEC/AGC/NS 是外放场景硬前提；48k 交给 worklet 降采到 16k */
function audioConstraints(): MediaTrackConstraints {
  return {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
    channelCount: 1,
    sampleRate: { ideal: 48000 },
  };
}

/** 监听麦克风插拔（§2.3：拔出 → 上层提示 + 回落默认设备重开）；返回解绑函数 */
export function onDeviceChange(cb: () => void): () => void {
  navigator.mediaDevices.addEventListener("devicechange", cb);
  return () => navigator.mediaDevices.removeEventListener("devicechange", cb);
}

export class MicCapture {
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private preRoll: Int16Array[] = [];
  private preRollHead = 0;

  /** 每帧回调（16k PCM16，60ms/帧；数组所有权归调用方） */
  onFrame: ((pcm: Int16Array) => void) | null = null;

  /** 当前麦克风流（供 WebRTC addTrack；未启动时为 null） */
  get stream(): MediaStream | null {
    return this.stream_ ?? null;
  }
  private stream_: MediaStream | null = null;

  async start(): Promise<void> {
    try {
      this.stream_ = await navigator.mediaDevices.getUserMedia({
        audio: audioConstraints(),
      });
    } catch (err) {
      throw mapGetUserMediaError(err);
    }

    this.ctx = new AudioContext();
    const workletUrl = new URL("./resample-worklet.js", import.meta.url);
    await this.ctx.audioWorklet.addModule(workletUrl);

    this.node = new AudioWorkletNode(this.ctx, "resample-capture");
    this.node.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
      const pcm = new Int16Array(e.data);
      // preRoll 环形缓冲：存副本（onFrame 的 buffer 已被转移，不能复用）
      if (this.preRoll.length < PRE_ROLL_FRAMES) {
        this.preRoll.push(pcm.slice());
      } else {
        this.preRoll[this.preRollHead] = pcm.slice();
        this.preRollHead = (this.preRollHead + 1) % PRE_ROLL_FRAMES;
      }
      this.onFrame?.(pcm);
    };

    this.ctx.createMediaStreamSource(this.stream!).connect(this.node);
    // 不连 destination：避免麦克风回灌到扬声器
  }

  /** 取当前 preRoll（按时间序，最多 600ms），取出后缓冲继续滚动 */
  takePreRoll(): Int16Array[] {
    if (this.preRoll.length < PRE_ROLL_FRAMES) return this.preRoll.splice(0);
    const ordered = [
      ...this.preRoll.slice(this.preRollHead),
      ...this.preRoll.slice(0, this.preRollHead),
    ];
    this.preRollHead = 0;
    return ordered;
  }

  async stop(): Promise<void> {
    this.onFrame = null;
    this.node?.port.close();
    this.node?.disconnect();
    this.node = null;
    this.stream_?.getTracks().forEach((t) => t.stop());
    this.stream_ = null;
    await this.ctx?.close();
    this.ctx = null;
  }
}
