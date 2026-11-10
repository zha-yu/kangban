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
 *   worker→main: ready / init_error{message} / speech_start / speech_end /
 *                runtime_error{message}（Day 7 D1 新增，协议只增不改）
 * 任何失败路径只 postMessage init_error，由主线程 vad.ts 转 VadUnavailableError
 * → realtime.ts 既有服务端 webrtcvad 兜底接管，主链路不断。
 * Day 7 D1（S1.3）：运行期 feedWindow 异常不再永久砖死（P1-5，经 runtime_error 上报）；
 * handleReset 说话态先补 speech_end（P3-37）；120s 零 speechstart 心跳遥测
 * （vad_no_speech_120s，任务卡 §2.2.3）。
 *
 * 参数口径与原 vad.ts 一致（adhoc-20261105 §4.2，threshold 见下）：
 * threshold 0.38（Day 7 J-D1 裁决）/ silence 0.8s / minSpeech 0.25s / maxSpeech 30s /
 * window 256 / 16k。
 */

"use strict";

const WINDOW_SIZE = 256;
/** Day 7 D1 关 P1-4：threshold 常量化。0.38 为先设值（裁决 J-D1，2026-11-10）；
 *  同日主理人真机复测后调整为 0.45（取代 J-D1 的 0.38——0.38 灵敏度过高易误触发，
 *  0.60 偏保守漏收音）。2026-11-10 主理人终审：跳过大样本近/远场标定，0.45 定稿
 *  （依据：D1-1/D1-2 真机 60 轮实测全部正常检出、无误触发报告，见联调记录）。 */
const VAD_THRESHOLD = 0.45;
/** Day 7 D1 心跳遥测：120s 零 speechstart 上报一次，便于现场收音复测 */
const NO_SPEECH_HEARTBEAT_MS = 120_000;

let detector = null;
let pending = new Float32Array(WINDOW_SIZE);
let used = 0;
let active = false;
/** 最近一次 speech_start 时刻与心跳是否已发（120s 零 speechstart 遥测锚点） */
let lastSpeechStartAt = Date.now();
let noSpeechHeartbeatFired = false;

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

/** Day 7 D1：运行期错误/遥测上行（协议新增类型；主线程 vad.ts 转发上层触发服务端兜底） */
function runtimeError(message) {
  self.postMessage({ type: "runtime_error", message: String(message) });
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
      threshold: VAD_THRESHOLD, // Day 7 D1：0.45 定稿（2026-11-10 主理人裁决，见文件头）
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
      // Day 7 D1 关 P1-5：喂窗前先重置缓冲——单次 acceptWaveform 异常不永久砖死
      //（否则 used 卡 256、后续帧全部丢失），异常经 runtime_error 上报主线程，
      // 上层 sendDiag("vad") 触发服务端 webrtcvad 兜底
      const windowSamples = pending;
      pending = new Float32Array(WINDOW_SIZE);
      used = 0;
      try {
        feedWindow(windowSamples);
      } catch (err) {
        runtimeError(`acceptWaveform 失败: ${err}`);
      }
      checkNoSpeechHeartbeat();
    }
  }
}

/** Day 7 D1 心跳遥测：非说话态下距上次 speech_start 超 120s → 上报一次（只报一次，
 *  下一次 speech_start 后重新计时），便于现场收音复测（任务卡 §2.2.3） */
function checkNoSpeechHeartbeat() {
  if (noSpeechHeartbeatFired || active) return;
  if (Date.now() - lastSpeechStartAt < NO_SPEECH_HEARTBEAT_MS) return;
  noSpeechHeartbeatFired = true;
  runtimeError("vad_no_speech_120s");
}

function feedWindow(samples) {
  if (!detector) return;
  detector.acceptWaveform(samples);
  // 关键：排空已完成语音段队列。sherpa-onnx 的段无人 pop 时，30s 环形缓冲
  // （bufferSizeInSeconds）填满后 isDetected() 冻结在 true，speech_end 永不触发
  // （debug H10：1 次 start 后状态机冻死）。本项目只用边沿信号，段样本经
  // WebRTC 轨上行，pop 丢弃即可（官方示例用法：while(!isEmpty()) pop()）。
  while (!detector.isEmpty()) detector.pop();
  const isActive = detector.isDetected();
  if (isActive !== active) {
    active = isActive;
    if (isActive) {
      lastSpeechStartAt = Date.now(); // 心跳锚点重置
      noSpeechHeartbeatFired = false; // 下一次零语音 120s 再报
    }
    self.postMessage({ type: isActive ? "speech_start" : "speech_end" });
  }
}

function handleReset() {
  // Day 7 D1 关 P3-37：说话态先补发 speech_end 清悬挂边沿再复位，
  // 防主线程「正在想」状态在 reset 后永不收口
  if (active) {
    active = false;
    self.postMessage({ type: "speech_end" });
  }
  lastSpeechStartAt = Date.now();
  noSpeechHeartbeatFired = false;
  // 新一轮会话前的状态复位（原 vad.ts reset 语义）
  if (detector && typeof detector.reset === "function") detector.reset();
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
