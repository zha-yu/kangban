/**
 * 健康采集答案回传 REST 客户端（day08/contracts.md §P-1）。
 * 错误分类风格对齐 voiceprint-client.ts：401 → 上层回登录；403 设备未绑长者
 * 独立分支；422 → 可重录话术；网络 / 超时 / 其他 → 通用话术。
 * Authorization 用设备 access_token（E4 签发）；elder_id / source 一律服务端
 * 解出与判定，客户端不传（AGENTS §4.9）。
 */

const REQUEST_TIMEOUT_MS = 15_000;
const GENERIC_UNAVAILABLE = "提交失败，请稍后再试";

export interface PadHealthSubmitBody {
  systolic?: number | null;
  diastolic?: number | null;
  sleep_hours?: number | null;
  mood_score?: number | null;
  complaint?: string | null;
}

/** POST /api/pad/health-records 200（§P-1）：形状与后端 HealthRecordCreated 一致 */
export interface PadHealthSubmitSuccess {
  record_id: string;
  severity: "green" | "yellow" | "red";
  rule_version: string;
  recorded_at: string;
  follow_up: string;
}

export type PadHealthSubmitError =
  | { kind: "auth" } // 401：token 失效，上层回登录
  | { kind: "device_unbound" } // 403 device_not_bound：设备未绑定长者
  | { kind: "invalid"; message: string } // 422：值域 / 至少一项校验失败
  | { kind: "unavailable"; message: string }; // 网络 / 超时 / 其他

/** 统一带 15s 超时与 Bearer 头（沿 voiceprint-client.ts 惯例） */
async function request(
  token: string,
  body: PadHealthSubmitBody,
): Promise<Response> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch("/api/pad/health-records", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
  } finally {
    clearTimeout(timer);
  }
}

/** 错误体话术提取：FastAPI 的 detail 是内部错误描述，绝不透出给长者 */
async function elderMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { message?: string };
    return body.message ?? GENERIC_UNAVAILABLE;
  } catch {
    return GENERIC_UNAVAILABLE;
  }
}

/**
 * 提交健康采集答案（§P-1）。
 * **不自动重试**（C4 同款口径）：模糊失败时上层给出「重新提交」按钮，
 * 由长者/护工明确操作后再试——重试语义按 H-2「追加新行不覆盖」如实登记。
 */
export async function submitHealthAnswers(
  token: string,
  body: PadHealthSubmitBody,
): Promise<PadHealthSubmitSuccess | PadHealthSubmitError> {
  let res: Response;
  try {
    res = await request(token, body);
  } catch {
    return { kind: "unavailable", message: GENERIC_UNAVAILABLE };
  }
  if (res.status === 401) return { kind: "auth" };
  if (res.status === 403) return { kind: "device_unbound" };
  // 422 是值域/至少一项校验失败：detail 是字段级校验说明，对长者只给固定可重试话术
  if (res.status === 422) return { kind: "invalid", message: "提交的内容没填完整，请检查后重试" };
  if (res.status === 200) {
    const data = (await res.json()) as Partial<PadHealthSubmitSuccess>;
    return {
      record_id: data.record_id ?? "",
      severity: data.severity ?? "green",
      rule_version: data.rule_version ?? "",
      recorded_at: data.recorded_at ?? "",
      follow_up: data.follow_up ?? "",
    };
  }
  return { kind: "unavailable", message: await elderMessage(res) };
}
