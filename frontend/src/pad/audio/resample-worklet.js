// AudioWorklet：设备采样率（48k ideal）→16k 线性插值重采样，输出 60ms/帧 PCM16（960 采样）。
// adhoc-20261105 §2.2：复用教师模块 spikes/reference/asr/src/asr/web/audio-worklet.js 模式，
// 按方案要求将「平均累加」替换为标准线性插值；帧长/preRoll 口径见 §4.2。
class ResampleCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.spacing = sampleRate / 16000; // 相邻输出的输入样本间隔（48k 时 3，44.1k 时 2.75625）
    this.prev = 0;
    this.curr = 0;
    this.phase = 1; // 距下一个输出的输入样本数，∈(0, spacing]
    this.out = new Int16Array(960);
    this.index = 0;
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const v of input) {
      this.curr = v;
      this.phase -= 1;
      while (this.phase <= 0) {
        // 输出点落在 prev..curr 之间，f∈(0,1]
        const f = 1 + this.phase;
        const s = this.prev + (this.curr - this.prev) * f;
        this.out[this.index++] = Math.round(Math.max(-1, Math.min(1, s)) * 32767);
        this.phase += this.spacing;
        if (this.index === 960) {
          this.port.postMessage(this.out.buffer, [this.out.buffer]); // 转移所有权，零拷贝
          this.out = new Int16Array(960);
          this.index = 0;
        }
      }
      this.prev = this.curr;
    }
    return true;
  }
}
registerProcessor("resample-capture", ResampleCapture);
