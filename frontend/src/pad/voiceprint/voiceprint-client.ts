/**
 * W3 声纹录入 REST 客户端（C4 契约：docs/team_tasks/day04/contracts.md §C4）。
 * 错误分类风格对齐 rest-client.ts：401 → 上层回登录；409 分重录 / 设备未绑；
 * 422 → 可重录话术；503 → body 长者可读降级话术且**绝不自动重试**（C4 明确）；
 * 网络 / 超时 → 通用话术。Authorization 用设备 access_token（E4 签发）；
 * elder_id 一律服务端由 device 解出，客户端不传（AGENTS §4.9）。
 */

const REQUEST_TIMEOUT_MS = 15_000;
const GENERIC_UNAVAILABLE = "声纹服务暂时不可用，请稍后再试";
const INVALID_AUDIO = "这段录音没有录好，请重新录这一段";

export type VoiceprintStatus = "none" | "partial" | "active";

/** GET /api/voiceprints/status 200（C4） */
export interface VoiceprintStatusSuccess {
  elder_id: string;
  status: VoiceprintStatus;
  registered: number[];
  version: number;
}

export type VoiceprintStatusError =
  | { kind: "auth" } // 401：上层回登录
  | { kind: "device_unbound" } // 409 device_unbound：设备未绑定长者（C4，task 前端三分支之一）
  | { kind: "unavailable"; message: string }; // 503 / 网络 / 超时 / 其他

/** POST /api/voiceprints/samples 200（C4）：3 段注册，幂等键 (elder_id, version, sample_index) */
export interface SampleUploadSuccess {
  elder_id: string;
  version: number;
  sample_index: 1 | 2 | 3;
  status: VoiceprintStatus;
}

export type SampleUploadError =
  | { kind: "auth" } // 401
  | { kind: "duplicate" } // 409 duplicate_sample：同 (elder_id, version, sample_index) 重复上传
  | { kind: "invalid"; message: string } // 422 validation_error：WAV 头非法 / 时长越界
  | { kind: "unavailable"; message: string }; // 503 vp_extract_unavailable / 网络 / 超时

export type DeleteVoiceprintResult =
  | { kind: "ok" } // 204：随时重录入口，服务端写 audit_logs（C4）
  | { kind: "auth" } // 401
  | { kind: "unavailable"; message: string }; // 网络 / 其他

/** 统一带 15s 超时与 Bearer 头；multipart 不手动设 Content-Type（交给浏览器带 boundary） */
async function request(
  url: string,
  token: string,
  method: "GET" | "POST" | "DELETE",
  body?: FormData,
): Promise<Response> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, {
      method,
      headers: { Authorization: `Bearer ${token}` },
      body,
      signal: ctrl.signal,
    });
  } finally {
    clearTimeout(timer);
  }
}

/**
 * 错误体话术提取：契约仅在 503 承诺携带长者可读话术（message 字段）；
 * FastAPI 的 detail 是内部错误描述，绝不透出给长者（沿 rest-client.ts 惯例）。
 */
async function elderMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { message?: string };
    return body.message ?? GENERIC_UNAVAILABLE;
  } catch {
    /* body 非 JSON，用通用话术 */
    return GENERIC_UNAVAILABLE;
  }
}

/** 查询声纹录入状态；409 device_unbound 单独分类，供主流程走「设备未绑长者」分支 */
export async function getVoiceprintStatus(
  token: string,
): Promise<VoiceprintStatusSuccess | VoiceprintStatusError> {
  let res: Response;
  try {
    res = await request("/api/voiceprints/status", token, "GET");
  } catch {
    return { kind: "unavailable", message: GENERIC_UNAVAILABLE };
  }
  if (res.status === 401) return { kind: "auth" };
  if (res.status === 409) return { kind: "device_unbound" };
  if (res.status === 200) {
    const body = (await res.json()) as Partial<VoiceprintStatusSuccess>;
    return {
      elder_id: body.elder_id ?? "",
      status: body.status ?? "none",
      registered: body.registered ?? [],
      version: body.version ?? 0,
    };
  }
  return { kind: "unavailable", message: await elderMessage(res) };
}

/** 上传一段声纹样本（multipart：audio + sample_index）；503 不重试，话术直接透出上层 */
export async function uploadSample(
  token: string,
  wav: Blob,
  sampleIndex: 1 | 2 | 3,
): Promise<SampleUploadSuccess | SampleUploadError> {
  const form = new FormData();
  form.append("audio", wav, "sample.wav");
  form.append("sample_index", String(sampleIndex));

  let res: Response;
  try {
    res = await request("/api/voiceprints/samples", token, "POST", form);
  } catch {
    return { kind: "unavailable", message: GENERIC_UNAVAILABLE };
  }
  if (res.status === 401) return { kind: "auth" };
  if (res.status === 409) return { kind: "duplicate" };
  if (res.status === 422) return { kind: "invalid", message: INVALID_AUDIO };
  if (res.status === 200) {
    const body = (await res.json()) as Partial<SampleUploadSuccess>;
    return {
      elder_id: body.elder_id ?? "",
      version: body.version ?? 0,
      sample_index: (body.sample_index ?? sampleIndex) as 1 | 2 | 3,
      status: body.status ?? "none",
    };
  }
  // 503 vp_extract_unavailable：body 携带长者可读降级话术（C4），原样透出、不自动重试
  return { kind: "unavailable", message: await elderMessage(res) };
}

/** 删除已录声纹（随时重录）；204 成功无 body */
export async function deleteVoiceprint(token: string): Promise<DeleteVoiceprintResult> {
  let res: Response;
  try {
    res = await request("/api/voiceprints", token, "DELETE");
  } catch {
    return { kind: "unavailable", message: GENERIC_UNAVAILABLE };
  }
  if (res.status === 204) return { kind: "ok" };
  if (res.status === 401) return { kind: "auth" };
  return { kind: "unavailable", message: await elderMessage(res) };
}
