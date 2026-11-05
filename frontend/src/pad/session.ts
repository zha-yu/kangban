/** 设备会话存取（E4）：token 只存 sessionStorage，不进 localStorage（防其他页面读取） */

const TOKEN_KEY = "kb_access_token";
const EXPIRES_KEY = "kb_expires_at";
const DEVICE_KEY = "kb_device";

export interface DeviceSession {
  access_token: string;
  /** 毫秒时间戳 */
  expires_at: number;
  device: { id: string; room_id: string; elder_id: string | null };
}

export function saveSession(s: DeviceSession): void {
  sessionStorage.setItem(TOKEN_KEY, s.access_token);
  sessionStorage.setItem(EXPIRES_KEY, String(s.expires_at));
  sessionStorage.setItem(DEVICE_KEY, JSON.stringify(s.device));
}

export function loadSession(): DeviceSession | null {
  const token = sessionStorage.getItem(TOKEN_KEY);
  const expiresAt = sessionStorage.getItem(EXPIRES_KEY);
  const device = sessionStorage.getItem(DEVICE_KEY);
  if (!token || !expiresAt || !device) return null;
  return {
    access_token: token,
    expires_at: Number(expiresAt),
    device: JSON.parse(device),
  };
}

/** 会话有效 = 有 token 且未过期（24h 设备会话）；无 refresh 通道则过期回登录页 */
export function sessionValid(): boolean {
  const s = loadSession();
  return s !== null && s.expires_at > Date.now();
}

export function clearSession(): void {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(EXPIRES_KEY);
  sessionStorage.removeItem(DEVICE_KEY);
}
