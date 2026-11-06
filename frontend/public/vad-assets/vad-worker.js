/**
 * 端侧 TEN-VAD Worker（f1-vad-worker隔离.md 方案 A）。
 *
 * 背景：/kws/ 与 /vad-assets/ 两套 sherpa-onnx emscripten 产物均为非 modularize 构建，
 * 顶层 `class ExitStatus` 等标识符在同一全局作用域二次声明即抛 SyntaxError——主线程
 * 先加载 KWS 后加载 VAD 必冲突。本 Worker 以独立全局作用域隔离 VAD 运行时，
 * 顺带把 wasm 编译/推理移出主线程（建连期不抢主线程，AC-42 ≤1.5s 预算）。
 *
 * 消息协议（见 docs/plans/day04/f1-vad-worker隔离.md §2.2）：
 *   main→worker: init / feed{pcm,transfer} / reset / free
 *   worker→main: ready / init_error{message} / speech_start / speech_end
 * 任何失败路径只 postMessage init_error，由主线程 vad.ts 转 VadUnavailableError
 * → realtime.ts 既有服务端 webrtcvad 兜底接管，主链路不断。
 *
 * 参数口径与原 vad.ts 一致（adhoc-20261105 §4.2）：
 * threshold 0.6 / silence 0.8s / minSpeech 0.25s / maxSpeech 30s / window 256 / 16k。
 */

"use strict";

const WINDOW_SIZE = 256;

let detector = null;
let pending = new Float32Array(WINDOW_SIZE);
let used = 0;
let active = false;

self.onmessage = (e) => {
  const msg = e.data || {};
  switch (msg.type) {
    case "init":
      init();
      break;
    case "feed":
      handleFeed(msg.pcm);
      break;
    case "reset":
      handleReset();
      break;
    case "free":
      handleFree();
      break;
    // 未知消息静默忽略：协议只增不改，旧主线程消息不致崩
  }
};

function initError(message) {
  self.postMessage({ type: "init_error", message: String(message) });
}

function init() {
  if (detector) {
    self.postMessage({ type: "ready" }); // 重复 init 幂等（StrictMode 双跑防重建）
    return;
  }
  // 胶水 API（createVad 定义）先行，importScripts 同步执行
  try {
    importScripts("/vad-assets/sherpa-onnx-vad.js");
  } catch (err) {
    initError(`VAD 胶水脚本加载失败: ${err}`);
    return;
  }
  // emscripten 约定：Module 配置须在主 wasm 脚本执行前挂到全局
  self.Module = {
    locateFile: (path) => `/vad-assets/${path}`,
    onRuntimeInitialized: () => {
      try {
        createDetector();
        self.postMessage({ type: "ready" });
      } catch (err) {
        initError(err);
      }
    },
    onAbort: (reason) => initError(`WASM 初始化中止: ${reason}`),
  };
  try {
    importScripts("/vad-assets/sherpa-onnx-wasm-main-vad.js");
  } catch (err) {
    // 12MB wasm 拉取失败或编译错误；主线程 30s 超时兜底同一结论
    initError(`VAD 运行时脚本加载失败: ${err}`);
  }
}

function createDetector() {
  const d = self.createVad(self.Module, {
    sileroVad: { model: "" },
    tenVad: {
      model: "ten-vad.onnx",
      threshold: 0.6, // adhoc-20261106：0.5→0.6 收窄触发面，只认近场清晰人声，减少远处他人语音误切段
      minSilenceDuration: 0.8,
      minSpeechDuration: 0.25,
      maxSpeechDuration: 30,
      windowSize: WINDOW_SIZE,
    },
    sampleRate: 16000,
    numThreads: 1, // 现网实证 WASM 多线程不受支持（GetNumThreads 警告），维持 1
    provider: "cpu",
    debug: 0,
    bufferSizeInSeconds: 30,
  });
  if (!d || !d.handle) throw new Error("TEN-VAD 检测器创建失败（无句柄）");
  detector = d;
}

function handleFeed(pcm) {
  if (!detector) return;
  // 16k PCM16 帧缓冲到 256 点窗口喂模型（原 vad.ts 同款状态机）
  for (let i = 0; i < pcm.length; i++) {
    pending[used++] = pcm[i] / 32768;
    if (used === WINDOW_SIZE) {
      feedWindow(pending);
      pending = new Float32Array(WINDOW_SIZE);
      used = 0;
    }
  }
}

function feedWindow(samples) {
  if (!detector) return;
  detector.acceptWaveform(samples);
  const isActive = detector.isDetected();
  if (isActive !== active) {
    active = isActive;
    self.postMessage({ type: isActive ? "speech_start" : "speech_end" });
  }
}

function handleReset() {
  // 新一轮会话前的状态复位（原 vad.ts reset 语义）
  if (detector && typeof detector.reset === "function") detector.reset();
  active = false;
  pending = new Float32Array(WINDOW_SIZE);
  used = 0;
}

function handleFree() {
  if (detector) {
    try {
      detector.free();
    } catch (_err) {
      // 释放失败不影响主线程：Worker 随后会被 terminate
    }
    detector = null;
  }
}
