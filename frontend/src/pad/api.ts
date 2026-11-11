import type { DeviceSession } from "./session";

/** E4 设备换证错误分类（HTTP 状态 → 长者可读话术 + 重试策略）。
 *  401 统一口径（2026-11-05 主理人裁决，任务卡 v1.2 / adhoc v1.4 已同步）：
 *  设备不存在/已锁定同码返回 401，防存在性探测；前端不区分具体原因。
 *  2026-11-11 裁决：取消设备密钥，device_id 直接换证。 */
export type LoginErrorCode = "unauthorized" | "rate_limited" | "network";

export class LoginError extends Error {
  constructor(
    public code: LoginErrorCode,
    message: string,
  ) {
    super(message);
  }
}

const ERROR_TEXT: Record<LoginErrorCode, string> = {
  unauthorized: "设备不存在或已被停用，请检查后重新输入",
  rate_limited: "尝试次数过多，请 1 分钟后再试",
  network: "网络连接失败，请检查网络后重试",
};

export function loginErrorText(code: LoginErrorCode): string {
  return ERROR_TEXT[code];
}

/** 换证入参（E4 契约）：device_id；elder_id 不由客户端传（禁区 9） */
export async function deviceLogin(deviceId: string): Promise<DeviceSession> {
  let res: Response;
  try {
    res = await fetch("/api/device-sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: deviceId }),
    });
  } catch {
    throw new LoginError("network", ERROR_TEXT.network);
  }

  // trace 留痕（不打印 token）
  const traceId = res.headers.get("x-trace-id");
  if (traceId) console.info(`[login] trace_id=${traceId}`);

  if (res.ok) {
    const body = (await res.json()) as {
      access_token: string;
      expires_in: number;
      device: { id: string; room_id: string; elder_id: string | null };
    };
    return {
      access_token: body.access_token,
      expires_at: Date.now() + body.expires_in * 1000,
      device: body.device,
    };
  }

  // 401 统一口径：设备不存在/已锁定都落 unauthorized
  // 422 归 unauthorized：device_id 格式非法（非 UUID）属输入错误，显示「网络失败」会误导排查方向
  const code: LoginErrorCode =
    res.status === 429
      ? "rate_limited"
      : res.status === 401 || res.status === 403 || res.status === 404 || res.status === 422
        ? "unauthorized"
        : "network";
  throw new LoginError(code, ERROR_TEXT[code]);
}

/* ---------- D3 一键求助（FR-ALT-06） ---------- */

export interface SosTriggerResult {
  event_id: string;
  order_id: string | null;
  created: boolean;
}

/** 求助发送失败（网络/401/403/5xx 统一收口，页面只给「再试一次」）。 */
export class SosRequestError extends Error {}

/** POST /api/pad/sos：设备令牌鉴权，elder_id 由服务端按绑定表解出（禁区 9）。 */
export async function triggerSos(token: string): Promise<SosTriggerResult> {
  let res: Response;
  try {
    res = await fetch("/api/pad/sos", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
  } catch {
    throw new SosRequestError("network");
  }

  if (!res.ok) {
    // 403 = 设备没绑长者（需工作人员处理）；其余按暂时失败给重试入口。
    throw new SosRequestError(res.status === 403 ? "device_not_bound" : "failed");
  }
  return (await res.json()) as SosTriggerResult;
}

/* ---------- mock 自测：无后端时验证三类错误 UI（?mock=1 启用） ---------- */

export const MOCK_ENABLED =
  import.meta.env.DEV && new URLSearchParams(location.search).has("mock");

/** mock 口径：device_id 输 401 / 429 触发对应错误，其余成功 */
export async function mockDeviceLogin(deviceId: string): Promise<DeviceSession> {
  await new Promise((r) => setTimeout(r, 300));
  if (!deviceId.trim()) throw new LoginError("unauthorized", ERROR_TEXT.unauthorized);
  if (deviceId.trim() === "401") throw new LoginError("unauthorized", ERROR_TEXT.unauthorized);
  if (deviceId.trim() === "429") throw new LoginError("rate_limited", ERROR_TEXT.rate_limited);
  return {
    access_token: "mock-token",
    expires_at: Date.now() + 86400_000,
    device: { id: deviceId, room_id: "mock-room", elder_id: null },
  };
}
