/**
 * T-D 端侧 TEN-VAD（sherpa-onnx WASM）适配层。
 * 参照教师模块 spikes/reference/asr/src/asr/web/vad/ten-vad.js 移植为 TS；
 * 参数与事件口径 adhoc-20261105 §4.2：threshold 0.5 / silence 0.8s / minSpeech 0.25s /
 * maxSpeech 30s（强制切段不丢中段）/ window 256。
 * 回落：加载或初始化失败抛 VadUnavailableError，上层捕获后走服务端 webrtcvad 兜底（字幕连续）。
 */

/* ---- sherpa-onnx 全局接口（由 /vad-assets 脚本注入） ---- */

interface SherpaVadDetector {
  handle: number;
  acceptWaveform(samples: Float32Array): void;
  isDetected(): boolean;
  isEmpty(): boolean;
  front(): { samples: Float32Array; start: number };
  pop(): void;
  flush?(): void;
  reset?(): void;
  free(): void;
}

declare global {
  // eslint-disable-next-line no-var
  var createVad: (
    module: unknown,
    config: Record<string, unknown>,
  ) => SherpaVadDetector;
  // eslint-disable-next-line no-var
  var Module: Record<string, unknown> | undefined;
}

const VAD_ASSETS = "/vad-assets";
const WINDOW_SIZE = 256;
/** WASM 加载超时（12MB wasm 本地加载，超时即回落服务端 VAD） */
const LOAD_TIMEOUT_MS = 20_000;

export class VadUnavailableError extends Error {
  constructor(message: string) {
    super(message);
  }
}

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src;
    el.onload = () => resolve();
    el.onerror = () => reject(new VadUnavailableError(`脚本加载失败: ${src}`));
    document.head.appendChild(el);
  });
}

async function loadSherpaRuntime(): Promise<void> {
  if (typeof globalThis.createVad === "function" && globalThis.Module) return;
  await loadScript(`${VAD_ASSETS}/sherpa-onnx-vad.js`);
  // emscripten 约定：Module 配置须在主 wasm 脚本加载前挂到全局
  globalThis.Module = {
    locateFile: (path: string) => `${VAD_ASSETS}/${path}`,
  };
  await Promise.race([
    new Promise<void>((resolve, reject) => {
      const m = (globalThis.Module = {
        ...globalThis.Module,
        onRuntimeInitialized: () => resolve(),
        onAbort: (reason: unknown) =>
          reject(new VadUnavailableError(`WASM 初始化中止: ${String(reason)}`)),
      });
      globalThis.Module = m;
      void loadScript(`${VAD_ASSETS}/sherpa-onnx-wasm-main-vad.js`).catch(reject);
    }),
    new Promise<never>((_, reject) =>
      setTimeout(
        () => reject(new VadUnavailableError("WASM 加载超时")),
        LOAD_TIMEOUT_MS,
      ),
    ),
  ]);
}

export interface TenVadCallbacks {
  onSpeechStart: () => void;
  onSpeechEnd: () => void;
}

export class TenVad {
  private detector: SherpaVadDetector | null = null;
  private pending = new Float32Array(WINDOW_SIZE);
  private used = 0;
  private active = false;

  private constructor(private callbacks: TenVadCallbacks) {}

  /** 加载 WASM 并构建检测器；任何失败抛 VadUnavailableError（上层回落服务端 VAD） */
  static async load(callbacks: TenVadCallbacks): Promise<TenVad> {
    try {
      await loadSherpaRuntime();
    } catch (err) {
      throw err instanceof VadUnavailableError
        ? err
        : new VadUnavailableError(String(err));
    }
    const vad = new TenVad(callbacks);
    vad.createDetector();
    return vad;
  }

  private createDetector(): void {
    let detector: SherpaVadDetector;
    try {
      detector = globalThis.createVad(globalThis.Module, {
        sileroVad: { model: "" },
        tenVad: {
          model: "ten-vad.onnx",
          threshold: 0.5,
          minSilenceDuration: 0.8,
          minSpeechDuration: 0.25,
          maxSpeechDuration: 30,
          windowSize: WINDOW_SIZE,
        },
        sampleRate: 16000,
        numThreads: 1,
        provider: "cpu",
        debug: 0,
        bufferSizeInSeconds: 30,
      });
    } catch (err) {
      throw new VadUnavailableError(`TEN-VAD 初始化失败: ${String(err)}`);
    }
    if (!detector.handle) throw new VadUnavailableError("TEN-VAD 初始化失败");
    this.detector = detector;
  }

  /** 端侧是否处于说话态（打断判定步骤 9 复用） */
  get speaking(): boolean {
    return this.active;
  }

  /** 输入 16k PCM16 帧（60ms/960 点），内部缓冲到 256 点窗口喂模型 */
  feed(pcm: Int16Array): void {
    if (!this.detector) throw new VadUnavailableError("检测器已释放");
    for (let i = 0; i < pcm.length; i++) {
      this.pending[this.used++] = pcm[i] / 32768;
      if (this.used === WINDOW_SIZE) {
        this.feedWindow(this.pending);
        this.pending = new Float32Array(WINDOW_SIZE);
        this.used = 0;
      }
    }
  }

  private feedWindow(samples: Float32Array): void {
    const d = this.detector;
    if (!d) return;
    d.acceptWaveform(samples);
    const active = d.isDetected();
    if (active !== this.active) {
      this.active = active;
      if (active) this.callbacks.onSpeechStart();
      else this.callbacks.onSpeechEnd();
    }
  }

  /** 释放并重建（新一轮会话前的状态复位） */
  reset(): void {
    if (!this.detector) return;
    if (typeof this.detector.reset === "function") this.detector.reset();
    this.active = false;
    this.used = 0;
  }

  free(): void {
    this.detector?.free();
    this.detector = null;
  }
}
