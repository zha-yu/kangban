// AudioWorklet：设备采样率（48k ideal）→16k 线性插值重采样，输出 60ms/帧 PCM16（960 采样）。
// adhoc-20261105 §2.2：复用教师模块 spikes/reference/asr/src/asr/web/audio-worklet.js 模式，
// 按方案要求将「平均累加」替换为标准线性插值；帧长/preRoll 口径见 §4.2。
//
// preRoll 轨上补发（A2 裁决 2026-11-05）：服务端不回溯 WebRTC 轨（实时单向流只能顺序消费），
// pad 在 speechstart 发出 asr_start 后，由本 worklet 把最近 600ms（10 帧）环形缓冲
// 经输出流回放补发，A1 侧将收到的补发帧拼进识别流。
// 输出复用策略：回放优先、实时样本进 FIFO 延迟补出——回放不吞实时音频，
// 补发结束后实时流无损接续（延迟 600ms，服务端按到达顺序拼接无感知）。
const PRE_ROLL_FRAMES = 10; // 600ms @60ms 帧，与 capture.ts PRE_ROLL_FRAMES 对齐
const FIFO_COMPACT = 8192; // FIFO 头指针超过此值时整理一次，防数组无限增长

class ResampleCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.spacing = sampleRate / 16000; // 相邻输出的输入样本间隔（48k 时 3，44.1k 时 2.75625）
    this.prev = 0;
    this.curr = 0;
    this.phase = 1; // 距下一个输出的输入样本数，∈(0, spacing]
    this.out = new Int16Array(960);
    this.index = 0;
    this.preRoll = []; // 最近 PRE_ROLL_FRAMES 个输出帧副本（时间序由 head 维护）
    this.preRollHead = 0;
    this.replayQueue = []; // flush_preRoll 后待回放的帧（时间序，优先输出）
    this.replayIdx = 0;
    this.liveFifo = []; // 回放期间暂存的实时样本，回放结束后依序补出
    this.liveHead = 0;
    this.port.onmessage = (e) => {
      if (e.data && e.data.cmd === "flush_preRoll") {
        const n = this.preRoll.length;
        const h = this.preRollHead;
        this.replayQueue =
          n < PRE_ROLL_FRAMES
            ? this.preRoll.slice()
            : [...this.preRoll.slice(h), ...this.preRoll.slice(0, h)];
        this.replayIdx = 0;
      }
    };
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    // 1. 重采样独立推进相位，实时样本入 FIFO（不因回放丢失）
    for (const v of input) {
      this.curr = v;
      this.phase -= 1;
      while (this.phase <= 0) {
        const f = 1 + this.phase;
        const s = this.prev + (this.curr - this.prev) * f;
        this.liveFifo.push(Math.round(Math.max(-1, Math.min(1, s)) * 32767));
        this.phase += this.spacing;
      }
      this.prev = this.curr;
    }
    // 2. 输出复用：先回放 preRoll 帧，再实时 FIFO
    while (this.index < 960) {
      let s;
      if (this.replayQueue.length) {
        const frame = this.replayQueue[0];
        s = frame[this.replayIdx++];
        if (this.replayIdx >= frame.length) {
          this.replayQueue.shift();
          this.replayIdx = 0;
        }
      } else if (this.liveHead < this.liveFifo.length) {
        s = this.liveFifo[this.liveHead++];
        if (this.liveHead >= FIFO_COMPACT) {
          this.liveFifo = this.liveFifo.slice(this.liveHead);
          this.liveHead = 0;
        }
      } else {
        break; // 本轮输入不足一帧，留待下个 process 继续（部分帧跨回调保持）
      }
      this.out[this.index++] = s;
      if (this.index === 960) {
        const copy = this.out.slice(); // 存副本供 preRoll 回放（out 将被转移）
        if (this.preRoll.length < PRE_ROLL_FRAMES) {
          this.preRoll.push(copy);
        } else {
          this.preRoll[this.preRollHead] = copy;
          this.preRollHead = (this.preRollHead + 1) % PRE_ROLL_FRAMES;
        }
        this.port.postMessage(this.out.buffer, [this.out.buffer]); // 转移所有权，零拷贝
        this.out = new Int16Array(960);
        this.index = 0;
      }
    }
    return true;
  }
}
registerProcessor("resample-capture", ResampleCapture);
