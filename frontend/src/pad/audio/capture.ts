/** T-C 麦克风采集封装：getUserMedia（constraints 按 adhoc §2.3）+ AudioWorklet 重采样
 *  + 600ms preRoll 环形缓冲（10 帧 × 60ms，保句首，§4.2）。 */

export const TARGET_RATE = 16000;
export const FRAME_SAMPLES = 960; // 60ms @16k
export const PRE_ROLL_FRAMES = 10; // 600ms preRoll

/** getUserMedia 错误映射（adhoc §2.4，长者可读话术） */
export type MicErrorCode =
  | "not_allowed"
  | "not_found"
  | "not_readable"
  | "virtual_only";
export const MIC_ERROR_TEXT: Record<MicErrorCode, string> = {
  not_allowed: "麦克风权限被拒绝：请点击地址栏图标允许后重试",
  not_found: "未检测到麦克风设备",
  not_readable: "麦克风被其他程序占用",
  virtual_only: "未检测到真实麦克风：当前输入是虚拟声卡，请接入麦克风设备后重试",
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

/** 虚拟声卡甄别（debug H9）：同传助手/BlackHole 等回环设备输出恒定满幅非语音信号，
 *  端侧 VAD 永不触发 → 「正在听」假死。优先物理麦克风；仅剩虚拟输入时快失败。 */
const VIRTUAL_INPUT_RE =
  /virtual|虚拟|loopback|blackhole|同传|aggregate|multi-output|infoflow/i;

async function pickPhysicalInput(): Promise<MediaTrackConstraints> {
  const devices = await navigator.mediaDevices.enumerateDevices();
  const inputs = devices.filter((d) => d.kind === "audioinput");
  const physical = inputs.find((d) => !VIRTUAL_INPUT_RE.test(d.label));
  if (!physical) {
    throw new MicCaptureError("virtual_only", MIC_ERROR_TEXT.virtual_only);
  }
  return { deviceId: { exact: physical.deviceId } };
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
      let picked: MediaTrackConstraints | null = null;
      try {
        picked = await pickPhysicalInput();
      } catch (err) {
        if (err instanceof MicCaptureError) throw err; // virtual_only 快失败
        throw err; // 枚举失败 → 走外层默认设备
      }
      this.stream_ = await navigator.mediaDevices.getUserMedia({
        audio: { ...audioConstraints(), ...picked },
      });
    } catch (err) {
      if (err instanceof MicCaptureError) throw err;
      // exact deviceId 因插拔失效 → 退回默认设备重试一次
      if (
        err instanceof DOMException &&
        err.name === "OverconstrainedError"
      ) {
        try {
          this.stream_ = await navigator.mediaDevices.getUserMedia({
            audio: audioConstraints(),
          });
        } catch (retryErr) {
          throw mapGetUserMediaError(retryErr);
        }
      } else {
        throw mapGetUserMediaError(err);
      }
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

  /** 触发 worklet 把 preRoll 环形缓冲经 WebRTC 轨回放补发（A2 裁决：服务端不回溯轨道，
   *  在 asr_start 发出后立即调用；主线程环形缓冲保留给 C3 降级 WAV 使用，两者独立） */
  flushPreRollToTrack(): void {
    this.node?.port.postMessage({ cmd: "flush_preRoll" });
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
