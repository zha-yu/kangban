/**
 * T-D 端侧 TEN-VAD（sherpa-onnx WASM）适配层 —— Worker 封装版（f1-vad-worker隔离.md 方案 A）。
 *
 * 背景：/kws/ 与 /vad-assets/ 两套 emscripten 产物均为非 modularize 构建，顶层
 * `class ExitStatus` 声明在同一全局作用域二次执行即抛 SyntaxError（KWS 先加载则 VAD 必炸）。
 * wasm 运行时改在 /vad-assets/vad-worker.js 的独立全局作用域内加载，与主线程 KWS 互不可见；
 * 编译/推理随之移出主线程（建连期不抢主线程，利好 AC-42 ≤1.5s）。
 * 回落：init_error / 脚本错误 / 30s 超时均抛 VadUnavailableError，
 * 上层（realtime.ts）捕获后走服务端 webrtcvad 兜底（字幕连续），主链路不断。
 * 参数与事件口径不变：adhoc-20261105 §4.2（threshold 0.5 / silence 0.8s / minSpeech 0.25s /
 * maxSpeech 30s / window 256 / 16k）。
 */

/** Worker→主线程消息（vad-worker.js 协议，见计划 §2.2） */
type VadWorkerOutMsg =
  | { type: "ready" }
  | { type: "init_error"; message: string }
  | { type: "speech_start" }
  | { type: "speech_end" };

/** 主线程→Worker 消息 */
type VadWorkerInMsg =
  | { type: "init" }
  | { type: "feed"; pcm: Int16Array }
  | { type: "reset" }
  | { type: "free" };

/** WASM 加载超时（12MB wasm，vite dev 无 gzip 首次拉取较慢；超时即回落服务端 VAD） */
const LOAD_TIMEOUT_MS = 30_000;

export class VadUnavailableError extends Error {
  constructor(message: string) {
    super(message);
  }
}

export interface TenVadCallbacks {
  onSpeechStart: () => void;
  onSpeechEnd: () => void;
}

export class TenVad {
  private worker: Worker | null = null;
  private active = false;

  private constructor(private callbacks: TenVadCallbacks) {}

  /** 建 Worker 并等待运行时就绪；任何失败抛 VadUnavailableError（上层回落服务端 VAD） */
  static async load(callbacks: TenVadCallbacks): Promise<TenVad> {
    const vad = new TenVad(callbacks);
    try {
      await vad.initWorker();
    } catch (err) {
      vad.free();
      throw err instanceof VadUnavailableError
        ? err
        : new VadUnavailableError(String(err));
    }
    return vad;
  }

  private initWorker(): Promise<void> {
    return new Promise((resolve, reject) => {
      let settled = false;
      let worker: Worker;
      try {
        worker = new Worker("/vad-assets/vad-worker.js");
      } catch (err) {
        reject(new VadUnavailableError(`VAD Worker 创建失败: ${String(err)}`));
        return;
      }
      const settle = (err: VadUnavailableError) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        worker.terminate();
        reject(err);
      };
      const timer = setTimeout(
        () => settle(new VadUnavailableError("VAD Worker 加载超时")),
        LOAD_TIMEOUT_MS,
      );
      worker.onmessage = (e: MessageEvent<VadWorkerOutMsg>) => {
        const msg = e.data;
        if (msg.type === "ready") {
          if (settled) return; // 超时后迟到的就绪：worker 已 terminate，弃用
          settled = true;
          clearTimeout(timer);
          this.worker = worker;
          this.attach(worker);
          resolve();
        } else if (msg.type === "init_error") {
          settle(new VadUnavailableError(msg.message));
        }
        // speech_* 在就绪前不可能出现，忽略
      };
      worker.onerror = (e) => {
        // 脚本级错误（资产 404、编译失败），与 init_error 互为兜底
        settle(new VadUnavailableError(`VAD Worker 脚本错误: ${e.message || "unknown"}`));
      };
      worker.postMessage({ type: "init" } satisfies VadWorkerInMsg);
    });
  }

  /** 就绪后接管消息：仅处理 speech 事件；运行期错误只留痕，由上层降级链路兜底 */
  private attach(worker: Worker): void {
    worker.onmessage = (e: MessageEvent<VadWorkerOutMsg>) => {
      const msg = e.data;
      if (msg.type === "speech_start") {
        this.active = true;
        this.callbacks.onSpeechStart();
      } else if (msg.type === "speech_end") {
        this.active = false;
        this.callbacks.onSpeechEnd();
      }
    };
    worker.onerror = (e) => {
      console.warn("[vad] worker 运行期错误:", e.message || "unknown");
    };
  }

  /** 端侧是否处于说话态（现网无同步消费方；跨线程镜像值，仅供展示级用途） */
  get speaking(): boolean {
    return this.active;
  }

  /** 输入 16k PCM16 帧（60ms/960 点）；buffer 每帧独立所有权（capture.ts transfer 语义），零拷贝转移给 worker */
  feed(pcm: Int16Array): void {
    if (!this.worker) throw new VadUnavailableError("检测器已释放");
    this.worker.postMessage(
      { type: "feed", pcm } satisfies VadWorkerInMsg,
      [pcm.buffer],
    );
  }

  /** 释放并重建（新一轮会话前的状态复位） */
  reset(): void {
    this.worker?.postMessage({ type: "reset" } satisfies VadWorkerInMsg);
    this.active = false;
  }

  free(): void {
    if (!this.worker) return;
    const w = this.worker;
    this.worker = null;
    try {
      w.postMessage({ type: "free" } satisfies VadWorkerInMsg);
    } catch {
      // worker 已死（如超时路径 terminate 过），忽略
    }
    // 兜底回收：即使 free 消息未及处理，terminate 保证 wasm 内存随线程释放
    w.terminate();
  }
}
