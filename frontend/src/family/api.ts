/**
 * 家属端认证 API 封装（task-05 F3）+ 业务数据双轨切换层（Day 5 · D-2）。
 *
 * 契约唯一依据：app/api/auth.py + app/api/schemas/auth.py + docs/接口契约_身份与权限.md §E1–E3；
 * 业务部分见 docs/接口契约_家属端数据.md v1.0 草案（待冻结）。
 * 注意：E1 实际端点是 `POST /api/sessions`（任务卡 §3 写的 `/api/sessions/login` 有笔误，
 * 按卡 §6「以 app/api/auth.py 为准」执行，差异已登记卡变更记录）。
 *
 * 本模块与 console/api.ts 为**独立同构实现**（task-05 §0：禁止互相 import 或 import src/pad/**）。
 * 同源 localStorage 共享，故所有 key 带入口前缀，避免双端互踢登录态。
 * 家属端 kind=guardian、role 为 None，角色显示只依赖 kind/name。
 *
 * 数据源分层（N6）：业务类型在 `./data-source.ts`（Day 8 S-11 后仅类型）；
 * 本文件保留认证域、类型导出与 5 个业务函数（直连冻结契约 v1.2 真实接口）。
 */

import {
  SCOPE_FORBIDDEN_TEXT,
  type AttentionEvent,
  type BoundElder,
  type DailyReportDetail,
  type DailyReportSummary,
  type FamilyNotification,
  type ListItems,
  type MedicationStats,
  type Paged,
} from "./data-source";

export { SCOPE_FORBIDDEN_TEXT };
export type {
  AttentionEvent,
  BoundElder,
  DailyReportDetail,
  DailyReportSummary,
  FamilyNotification,
  ListItems,
  MedicationStats,
  Paged,
};

const STORAGE_PREFIX = "kb_family";
const ACCESS_KEY = `${STORAGE_PREFIX}_access_token`;
const REFRESH_KEY = `${STORAGE_PREFIX}_refresh_token`;
const PROFILE_KEY = `${STORAGE_PREFIX}_profile`;

/** 登录主体信息（E1 principal / E3 身份详情共用的展示字段） */
export interface Principal {
  kind: "staff" | "guardian";
  id: string;
  role: string | null;
  name: string;
}

/** E3 当前身份详情（scope_elder_ids = elder_guardians 绑定的长者，裁决 A-01） */
export interface CurrentIdentity extends Principal {
  scope_elder_ids: string[];
}

/** 统一错误文案（task-05 §3 要点 4：文案用用户语言，禁止回显 trace_id / 内部细节） */
export type ApiErrorKind = "invalid" | "network" | "forbidden";
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

/** 角色显示名：家属端主体恒为 guardian（task-05 §3 要点 3） */
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

/** 退出登录：清本入口 localStorage 回登录页（task-05 §3 要点 6） */
export function logout(): void {
  clearSession();
}

// ---------------------------------------------------------------------------
// 家属端业务数据（契约：docs/接口契约_家属端数据.md v1.2 已冻结）
// Day 8 S-11 收口：五端点交付＋冻结＋联调通过，Mock 分支与开关已删除（同提交登记）。
// ---------------------------------------------------------------------------

/** 业务接口响应 → ApiError（401 凭证失效 / 403 越权 / 其余按网络错，不回显内部细节） */
async function ensureOk(res: Response): Promise<void> {
  if (res.ok) return;
  if (res.status === 401) throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
  if (res.status === 403) throw new ApiError("forbidden", SCOPE_FORBIDDEN_TEXT);
  throw new ApiError("network", NETWORK_ERROR_TEXT);
}

// ---- 业务 fetch（路径严格对齐冻结契约 §4） ----

export async function fetchBoundElders(): Promise<BoundElder[]> {
  // F0 GET /api/family/elders（契约 §4 F0 只返回 {items}）
  const res = await authedFetch("/api/family/elders");
  await ensureOk(res);
  const data = (await res.json()) as ListItems<BoundElder>;
  return data.items ?? [];
}

export async function fetchReports(elderId: string): Promise<DailyReportSummary[]> {
  // F1 GET /api/family/elders/{elder_id}/reports（分页 1/20，上限 100，S-10）
  const res = await authedFetch(
    `/api/family/elders/${encodeURIComponent(elderId)}/reports?page=1&page_size=100`,
  );
  await ensureOk(res);
  const data = (await res.json()) as Paged<DailyReportSummary>;
  return data.items ?? [];
}

export async function fetchReport(reportId: string): Promise<DailyReportDetail> {
  // F2 GET /api/family/reports/{report_id}
  const res = await authedFetch(`/api/family/reports/${encodeURIComponent(reportId)}`);
  await ensureOk(res);
  return (await res.json()) as DailyReportDetail;
}

export async function fetchNotifications(): Promise<FamilyNotification[]> {
  // N1 GET /api/family/notifications（分页 1 起、上限 100）
  const res = await authedFetch("/api/family/notifications?page=1&page_size=100&unread_only=false");
  await ensureOk(res);
  const data = (await res.json()) as Paged<FamilyNotification>;
  return data.items ?? [];
}

/** N4 标记已读（幂等：重复调用返回原 read_at，且只写 read_at、不动 status） */
export async function markNotificationRead(notificationId: string): Promise<string> {
  const res = await authedFetch(
    `/api/family/notifications/${encodeURIComponent(notificationId)}/read`,
    { method: "POST" },
  );
  await ensureOk(res);
  const data = (await res.json()) as { notification_id: string; read_at: string };
  return data.read_at;
}
