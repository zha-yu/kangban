/**
 * 照护台认证 API 封装（task-05 F3）。
 *
 * 契约唯一依据：app/api/auth.py + app/api/schemas/auth.py + docs/接口契约_身份与权限.md §E1–E3。
 * 注意：E1 实际端点是 `POST /api/sessions`（任务卡 §3 写的 `/api/sessions/login` 有笔误，
 * 按卡 §6「以 app/api/auth.py 为准」执行，差异已登记卡变更记录）。
 *
 * 本模块与 family/api.ts 为**独立同构实现**（task-05 §0：禁止互相 import 或 import src/pad/**）。
 * 同源 localStorage 共享，故所有 key 带入口前缀，避免双端互踢登录态。
 */

const STORAGE_PREFIX = "kb_console";
const ACCESS_KEY = `${STORAGE_PREFIX}_access_token`;
const REFRESH_KEY = `${STORAGE_PREFIX}_refresh_token`;
const PROFILE_KEY = `${STORAGE_PREFIX}_profile`;

/**
 * Mock 开关（Day 8 S6a → S6 翻转）：健康/复核/告警三组接口已切真实接口为默认；
 * 仅显式设 `VITE_USE_MOCK_API=1` 时回退 Mock 内存层（演示/兜底用）。
 * 工单/药单/长者等已交付接口不受此开关影响（本来就走真实接口）。
 */
export const USE_MOCK_API = import.meta.env.VITE_USE_MOCK_API === "1";

/** 登录主体信息（E1 principal / E3 身份详情共用的展示字段） */
export interface Principal {
  kind: "staff" | "guardian";
  id: string;
  role: string | null;
  name: string;
}

/** E3 当前身份详情 */
export interface CurrentIdentity extends Principal {
  scope_elder_ids: string[];
}

/** 统一错误文案（task-05 §3 要点 4：文案用用户语言，禁止回显 trace_id / 内部细节） */
export type ApiErrorKind = "invalid" | "network";
export class ApiError extends Error {
  constructor(
    public readonly kind: ApiErrorKind,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const NETWORK_ERROR_TEXT = "网络异常，请稍后重试";
export const INVALID_CREDENTIAL_TEXT = "手机号或密码不正确";

/** 角色显示名（task-05 §3 要点 3：护工/医生/管理员/家属） */
export function roleLabel(principal: Principal): string {
  if (principal.kind === "guardian") return "家属";
  const labels: Record<string, string> = {
    care_worker: "护工",
    doctor: "医生",
    admin: "管理员",
  };
  return labels[principal.role ?? ""] ?? principal.role ?? "员工";
}

// ---------------------------------------------------------------------------
// 登录态存取（localStorage，入口前缀隔离）
// ---------------------------------------------------------------------------

function readAccess(): string | null {
  return localStorage.getItem(ACCESS_KEY);
}

function readRefresh(): string | null {
  return localStorage.getItem(REFRESH_KEY);
}

function readProfile(): Principal | null {
  const raw = localStorage.getItem(PROFILE_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Principal;
  } catch {
    return null;
  }
}

function saveSession(access: string, refresh: string, principal: Principal) {
  localStorage.setItem(ACCESS_KEY, access);
  localStorage.setItem(REFRESH_KEY, refresh);
  localStorage.setItem(PROFILE_KEY, JSON.stringify(principal));
}

function saveAccess(access: string) {
  localStorage.setItem(ACCESS_KEY, access);
}

/** 清空本入口全部登录态（退出 / refresh 失效时调用） */
export function clearSession() {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
  localStorage.removeItem(PROFILE_KEY);
}

/** 本地是否有登录态（刷新页面后先按此决定是否走 fetchCurrent 恢复） */
export function hasSession(): boolean {
  return Boolean(readAccess() || readRefresh());
}

export function cachedProfile(): Principal | null {
  return readProfile();
}

// ---------------------------------------------------------------------------
// refresh 自动续期（同一时刻只允许一个 refresh 在途，其余等待复用结果）
// ---------------------------------------------------------------------------

let refreshInFlight: Promise<string> | null = null;

async function refreshOnce(): Promise<string> {
  if (refreshInFlight) return refreshInFlight;
  const refreshToken = readRefresh();
  if (!refreshToken) {
    return Promise.reject(new ApiError("invalid", INVALID_CREDENTIAL_TEXT));
  }
  refreshInFlight = (async () => {
    try {
      const res = await fetch("/api/sessions/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!res.ok) {
        // refresh 也失效（401 类型不符/过期/token_version 不匹配）→ 清态回登录页
        clearSession();
        throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
      }
      const data = (await res.json()) as { access_token: string };
      saveAccess(data.access_token);
      return data.access_token;
    } catch (err) {
      if (err instanceof ApiError) throw err;
      clearSession();
      throw new ApiError("network", NETWORK_ERROR_TEXT);
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

/** 带鉴权请求：access 过期自动 refresh 后重放原请求一次（task-05 F3 验收） */
async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const access = readAccess();
  let res = await fetch(path, {
    ...init,
    headers: {
      ...(init.headers ?? {}),
      ...(access ? { Authorization: `Bearer ${access}` } : {}),
    },
  });
  if (res.status === 401 && readRefresh()) {
    const newAccess = await refreshOnce();
    res = await fetch(path, {
      ...init,
      headers: {
        ...(init.headers ?? {}),
        Authorization: `Bearer ${newAccess}`,
      },
    });
  }
  return res;
}

// ---------------------------------------------------------------------------
// E1：POST /api/sessions —— 账号密码登录（staff 与 guardian 共用）
// ---------------------------------------------------------------------------

interface LoginResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  principal: Principal;
}

export async function login(phone: string, password: string): Promise<Principal> {
  let res: Response;
  try {
    res = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone, password }),
    });
  } catch {
    throw new ApiError("network", NETWORK_ERROR_TEXT);
  }
  if (!res.ok) {
    // 401 密码错/账号 disabled；其余（422/5xx）按固定文案，不回显内部细节
    throw new ApiError(
      res.status === 401 ? "invalid" : "network",
      res.status === 401 ? INVALID_CREDENTIAL_TEXT : NETWORK_ERROR_TEXT,
    );
  }
  const data = (await res.json()) as LoginResponse;
  saveSession(data.access_token, data.refresh_token, data.principal);
  return data.principal;
}

// ---------------------------------------------------------------------------
// E3：GET /api/sessions/current —— 拉取当前身份（刷新页面恢复登录态用）
// ---------------------------------------------------------------------------

export async function fetchCurrent(): Promise<CurrentIdentity> {
  let res: Response;
  try {
    res = await authedFetch("/api/sessions/current");
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError("network", NETWORK_ERROR_TEXT);
  }
  if (!res.ok) {
    if (res.status === 401) {
      // refresh 已在 authedFetch 内尝试并失败 → 登录态已清
      throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
    }
    throw new ApiError("network", NETWORK_ERROR_TEXT);
  }
  return (await res.json()) as CurrentIdentity;
}

/** 通用鉴权 GET（task-07 F3）：复用 authedFetch（401 自动续期重放），错误归一为 ApiError */
export async function apiGet<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await authedFetch(path);
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError("network", NETWORK_ERROR_TEXT);
  }
  if (!res.ok) {
    // 401：refresh 已在 authedFetch 内尝试并失败 → 登录态已清；其余统一网络文案
    if (res.status === 401) {
      throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
    }
    throw new ApiError("network", NETWORK_ERROR_TEXT);
  }
  return (await res.json()) as T;
}

/** 鉴权原始 GET（Day 9 C6：审计 CSV 导出等二进制/非 JSON 响应）：返回原始 Response，
 *  401 已归一，404/403 等状态码由调用方分支（stats-api assertAuditOk） */
export async function apiGetRaw(path: string): Promise<Response> {
  let res: Response;
  try {
    res = await authedFetch(path);
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError("network", NETWORK_ERROR_TEXT);
  }
  if (res.status === 401) {
    throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
  }
  return res;
}

/** 带状态的鉴权 GET：4xx/5xx 不再吞成「网络异常」，把状态码与错误码抛给调用方分支 */
export async function apiGetWithStatus<T>(path: string): Promise<{ status: number; body: T | null }> {
  let res: Response;
  try {
    res = await authedFetch(path);
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError("network", NETWORK_ERROR_TEXT);
  }
  if (res.status === 401) {
    throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
  }
  if (!res.ok) return { status: res.status, body: null };
  return { status: res.status, body: (await res.json()) as T };
}

/** 通用鉴权 POST（task-08 F2）：返回原始 Response，业务码分支由调用方按契约处理 */
export async function apiPost(path: string, body: unknown): Promise<Response> {
  let res: Response;
  try {
    res = await authedFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError("network", NETWORK_ERROR_TEXT);
  }
  if (res.status === 401) {
    // refresh 已在 authedFetch 内尝试并失败 → 登录态已清
    throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
  }
  return res;
}

/** 通用鉴权 PUT（Day 6 E20 员工改绑长者）：与 apiPost 完全同构，业务码分支由调用方按契约处理 */
export async function apiPut(path: string, body: unknown): Promise<Response> {
  let res: Response;
  try {
    res = await authedFetch(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError("network", NETWORK_ERROR_TEXT);
  }
  if (res.status === 401) {
    // refresh 已在 authedFetch 内尝试并失败 → 登录态已清
    throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
  }
  return res;
}

/** 通用鉴权 PATCH（房间/床位管理）：返回原始 Response，204 也按 ok 正常返回 */
export async function apiPatch(path: string, body: unknown): Promise<Response> {
  let res: Response;
  try {
    res = await authedFetch(path, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError("network", NETWORK_ERROR_TEXT);
  }
  if (res.status === 401) {
    // refresh 已在 authedFetch 内尝试并失败 → 登录态已清
    throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
  }
  return res;
}

/** 通用鉴权 DELETE：返回原始 Response，204 也按 ok 正常返回 */
export async function apiDelete(path: string): Promise<Response> {
  let res: Response;
  try {
    res = await authedFetch(path, { method: "DELETE" });
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError("network", NETWORK_ERROR_TEXT);
  }
  if (res.status === 401) {
    // refresh 已在 authedFetch 内尝试并失败 → 登录态已清
    throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
  }
  return res;
}

/** 退出登录：清本入口 localStorage 回登录页（task-05 §3 要点 6） */
export function logout(): void {
  clearSession();
}
