/**
 * W2 唤醒词：sherpa-onnx KWS wasm 封装（纯浏览器端，音频不出端）。
 *
 * - 运行时资产位于 /kws/（.gitignore，体积 ~17MB；重建方法见 scripts/build-kws-wasm.md）
 * - 模型：zipformer wenetspeech 3.3M mobile int8（encoder int8 3.8M / decoder fp32 660K / joiner int8 64K）
 * - 唤醒词「小伴」以内联 keywords 传入（拼音 token 序列），无需改模型词表
 * - R-5 口径：加载失败/超时 → loadKwsDetector() 返回 null，UI 只保留「呼叫按钮」入口（FR-UI-09 路径复用，两入口正交）
 * - 命中即触发 onHit 并自动重臂（standby 期持续监听；连接建立后由调用方停喂即可静默）
 */

/** 「小伴」拼音 token 序列（tokens.txt 词表内存在：x iǎo b àn） */
export const KWS_KEYWORD_PINYIN = "x iǎo b àn @小伴";

const KWS_ASSET_PREFIX = "/kws/";
const LOAD_TIMEOUT_MS = 30_000; // dev 首次拉 12MB wasm + 4.5MB data 较慢；nginx gzip 下更快

/* ---- Emscripten 非 modularize 产物的最小类型声明 ---- */
interface EmscriptenModuleConfig {
  locateFile: (path: string, prefix: string) => string;
  onRuntimeInitialized?: () => void;
  setStatus?: (s: string) => void;
}
interface KwsStream {
  acceptWaveform(sampleRate: number, samples: Float32Array): void;
}
interface KwsRecognizer {
  createStream(): KwsStream;
  isReady(stream: KwsStream): boolean;
  decode(stream: KwsStream): void;
  getResult(stream: KwsStream): { keyword: string };
  reset(stream: KwsStream): void;
}

declare global {
  interface Window {
    Module?: EmscriptenModuleConfig;
    createKws?: (mod: EmscriptenModuleConfig, config?: unknown) => KwsRecognizer;
  }
}

export interface KwsDetector {
  /** 喂一帧 16k Int16 PCM（如 MicCapture 的 60ms/960 样点帧），内部检测到「小伴」时回调 onHit */
  feed(pcm: Int16Array): void;
  /** 释放回调引用（wasm 单例与页面同生命周期，无需真正卸载） */
  close(): void;
  onHit: (() => void) | null;
}

let loadPromise: Promise<KwsDetector | null> | null = null;
let recognizer: KwsRecognizer | null = null;
let stream: KwsStream | null = null;

/** 动态注入 <script>，resolve 于 onload / reject 于 onerror；同 src 已存在则直接成功（StrictMode 双跑防重复执行） */
function injectScript(src: string): Promise<void> {
  if (document.querySelector(`script[src="${src}"]`)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error(`script load failed: ${src}`));
    document.head.appendChild(el);
  });
}

/** 单例加载：成功返回 KwsDetector，失败/超时返回 null（R-5 降级口径） */
export function loadKwsDetector(): Promise<KwsDetector | null> {
  loadPromise ??= (async () => {
    // 超时后 runtime 可能仍会就绪（12MB wasm 慢加载）；settled 保证超时路径
    // 不再 createKws、也不清 window.Module（后续就绪时引用它无害）
    let settled = false;
    try {
      // 胶水 API（createKws 定义）先行加载，与运行时互不依赖
      if (!window.createKws) {
        await injectScript(`${KWS_ASSET_PREFIX}sherpa-onnx-kws.js`);
      }
      // main.js 是非 modularize 产物，执行时读取已存在的全局 Module —— 必须先设再加载
      const runtimeReady = new Promise<void>((resolve, reject) => {
        window.Module = {
          locateFile: (p) => KWS_ASSET_PREFIX + p, // 同时定向 .wasm 与 .data
          onRuntimeInitialized: resolve,
        };
        injectScript(`${KWS_ASSET_PREFIX}sherpa-onnx-wasm-kws-main.js`).catch(reject);
      });
      await Promise.race([
        runtimeReady,
        new Promise<never>((_, rej) => setTimeout(() => rej(new Error("kws runtime timeout")), LOAD_TIMEOUT_MS)),
      ]);
      if (settled || !window.createKws) throw new Error("createKws glue missing");

      recognizer = window.createKws(window.Module!, {
        featConfig: { sampleRate: 16000, featureDim: 80 },
        modelConfig: {
          // 胶水的 initModelConfig 要求 transducer 嵌套结构；.data 经 --preload-file assets@.
          // 挂在 MEMFS 根，路径即文件名
          transducer: {
            encoder: "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            decoder: "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
            joiner: "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
          },
          tokens: "tokens.txt",
          numThreads: 2,
          provider: "cpu",
        },
        keywords: KWS_KEYWORD_PINYIN, // 内联唤醒词，见 createKws 签名（config 优先于词表文件）
        maxActivePaths: 4,
        numTrailingBlanks: 1,
        keywordsScore: 2.5,
        keywordsThreshold: 0.25,
      });
      stream = recognizer.createStream();
      return {
        onHit: null,
        feed(pcm: Int16Array) {
          if (!recognizer || !stream) return;
          const f32 = new Float32Array(pcm.length);
          for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
          stream.acceptWaveform(16000, f32);
          while (recognizer.isReady(stream)) recognizer.decode(stream);
          const r = recognizer.getResult(stream);
          if (r.keyword && r.keyword.length > 0) {
            recognizer.reset(stream); // 重臂，standby 期继续监听
            this.onHit?.();
          }
        },
        close() {
          this.onHit = null;
        },
      } satisfies KwsDetector;
    } catch (e) {
      console.warn("[kws] 不可用，落呼叫按钮降级：", e); // R-5：不阻塞主流程
      return null;
    } finally {
      settled = true;
    }
  })();
  return loadPromise;
}
