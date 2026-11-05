/**
 * T-G 降级 REST 客户端（C3 契约）：WebRTC 主链路不可用时，按住说话整段上传。
 * 错误四类均不白屏：401 → 回登录；413 → 超长提示；503 → body 降级话术；网络/其他 → 通用话术。
 */
import { assembleWav } from "./audio/wav";

const UTTERANCE_TIMEOUT_MS = 15_000;
const GENERIC_UNAVAILABLE = "语音服务暂时不可用，请稍后再试";
const TOO_LONG = "说话有点长，请说 30 秒以内的内容";

export interface UtteranceSuccess {
  text: string;
  replyText: string;
  replyAudioUrl: string | null;
}

export type UtteranceError =
  | { kind: "auth"; message?: undefined } // 401：上层回登录
  | { kind: "too_long"; message: string } // 413
  | { kind: "unavailable"; message: string } // 503 / 网络 / 其他
  | { kind: "empty"; message?: undefined }; // 按住时间过短无音频

/** frames：按时间序的 16k PCM16 帧（调用方先放 preRoll 再放按住期间帧） */
export async function sendUtterance(
  token: string,
  frames: Int16Array[],
  durationMs: number,
): Promise<UtteranceSuccess | UtteranceError> {
  if (frames.length === 0) return { kind: "empty" };
  const wav = assembleWav(frames, durationMs);
  const form = new FormData();
  form.append("audio", wav, "utterance.wav");
  form.append("duration_ms", String(Math.round(durationMs)));

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), UTTERANCE_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch("/api/audio/utterance", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
      signal: ctrl.signal,
    });
  } catch {
    return { kind: "unavailable", message: GENERIC_UNAVAILABLE };
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401) return { kind: "auth" };
  if (res.status === 413) return { kind: "too_long", message: TOO_LONG };
  if (res.status === 200) {
    const body = (await res.json()) as {
      text?: string;
      reply?: { text?: string; audio_url?: string | null };
    };
    return {
      text: body.text ?? "",
      replyText: body.reply?.text ?? "",
      replyAudioUrl: body.reply?.audio_url ?? null,
    };
  }
  // 503 及其他：body 携带降级话术（契约仅约定 message 字段；FastAPI 的
  // detail 是内部错误描述如 "Not Found"，绝不能透出给长者）
  let message = GENERIC_UNAVAILABLE;
  try {
    const body = (await res.json()) as { message?: string };
    message = body.message ?? GENERIC_UNAVAILABLE;
  } catch {
    /* body 非 JSON，用通用话术 */
  }
  return { kind: "unavailable", message };
}
