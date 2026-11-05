/**
 * T-G 降级链路：16k/16bit/单声道 WAV 组装（C3 契约：整段上传，含 preRoll）。
 * 输入为 resample-worklet 输出的 60ms/帧 PCM16 数组（按时间序）。
 */
const SAMPLE_RATE = 16000;
const BYTES_PER_SAMPLE = 2;

export function assembleWav(frames: Int16Array[], durationMs?: number): Blob {
  let totalSamples = 0;
  for (const f of frames) totalSamples += f.length;
  const dataBytes = totalSamples * BYTES_PER_SAMPLE;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);

  // RIFF / fmt / data 头（44 字节，PCM 单声道）
  const writeStr = (offset: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true); // fmt 块长
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // 单声道
  view.setUint32(24, SAMPLE_RATE, true);
  view.setUint32(28, SAMPLE_RATE * BYTES_PER_SAMPLE, true); // byte rate
  view.setUint16(32, BYTES_PER_SAMPLE, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeStr(36, "data");
  view.setUint32(40, dataBytes, true);

  let offset = 44;
  for (const f of frames) {
    for (let i = 0; i < f.length; i++, offset += 2) {
      view.setInt16(offset, f[i], true);
    }
  }
  void durationMs; // duration_ms 由调用方直接放进表单（客户端计时，服务端仅入 trace）
  return new Blob([buffer], { type: "audio/wav" });
}
