/**
 * 家属端认证 API 封装（task-05 F3）。
 *
 * 契约唯一依据：app/api/auth.py + app/api/schemas/auth.py + docs/接口契约_身份与权限.md §E1–E3。
 * 注意：E1 实际端点是 `POST /api/sessions`（任务卡 §3 写的 `/api/sessions/login` 有笔误，
 * 按卡 §6「以 app/api/auth.py 为准」执行，差异已登记卡变更记录）。
 *
 * 本模块与 console/api.ts 为**独立同构实现**（task-05 §0：禁止互相 import 或 import src/pad/**）。
 * 同源 localStorage 共享，故所有 key 带入口前缀，避免双端互踢登录态。
 * 家属端 kind=guardian、role 为 None，角色显示只依赖 kind/name。
 */

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
// 家属端业务数据（契约：docs/接口契约_家属端数据.md v1.0 草案）
// ---------------------------------------------------------------------------

/**
 * S-11：后端 daily_reports / notifications 属 Day 8 批次尚未交付，
 * 前端 Mock 先行——本常量切 false 后走真实接口，联调通过后删除 mock 分支。
 */
export const USE_MOCK = true;

/** 403 越权文案（FR-RPT-02：家属只能查看绑定长者，服务端 403 + 审计） */
export const SCOPE_FORBIDDEN_TEXT = "无权查看该长者的数据";

/** 用药四类统计（契约 S-01：键名与用药回执终态逐字对齐，禁止合并为「实际漏服」） */
export interface MedicationStats {
  taken_reported: number;
  refused: number;
  missed_unconfirmed: number;
  notification_failed: number;
}

/** F0 授权长者（切换器数据源，服务端 WHERE guardian_id=:current AND status='active'） */
export interface BoundElder {
  elder_id: string;
  name: string;
  relation: string;
}

/** 需关注事件（F2 详情内，契约 S-09 服务端 JOIN 组装） */
export interface AttentionEvent {
  level: "red" | "yellow";
  type: string;
  brief: string;
  occurred_at: string;
}

/** F1 日报列表项（同 report_date 只出最新版，契约 S-07） */
export interface DailyReportSummary {
  report_id: string;
  elder_id: string;
  report_date: string;
  version: number;
  status: "normal" | "away_notice";
  summary_preview: string;
  audio_url: string | null;
  pushed_at: string | null;
}

/** F2 日报详情（含固定声明「教学模拟信息，不构成医疗诊断」） */
export interface DailyReportDetail extends Omit<DailyReportSummary, "summary_preview"> {
  summary_text: string;
  medication_stats: MedicationStats;
  health_summary: string;
  attention_events: AttentionEvent[];
}

/** N1 通知项（家属侧 P0 只见 channel=inapp，契约 §1.2） */
export interface FamilyNotification {
  notification_id: string;
  type: string;
  payload: { elder_name: string; event_brief: string; report_id: string | null };
  channel: "inapp" | "webpush";
  status: "generated" | "pending" | "delivered" | "acknowledged" | "failed";
  is_mock: boolean;
  retry_count: number;
  created_at: string;
  sent_at: string | null;
  read_at: string | null;
}

interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** 业务接口响应 → ApiError（401 凭证失效 / 403 越权 / 其余按网络错，不回显内部细节） */
async function ensureOk(res: Response): Promise<void> {
  if (res.ok) return;
  if (res.status === 401) throw new ApiError("invalid", INVALID_CREDENTIAL_TEXT);
  if (res.status === 403) throw new ApiError("forbidden", SCOPE_FORBIDDEN_TEXT);
  throw new ApiError("network", NETWORK_ERROR_TEXT);
}

// ---- mock 数据（S-11：结构与真实接口响应逐字段一致） ----

const MOCK_ELDERS: BoundElder[] = [
  { elder_id: "e1000000-0000-0000-0000-000000000001", name: "张桂芬", relation: "母亲" },
  { elder_id: "e2000000-0000-0000-0000-000000000002", name: "李建国", relation: "父亲" },
];

function localDate(daysAgo: number): string {
  const d = new Date();
  d.setDate(d.getDate() - daysAgo);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function isoAgo(hours: number): string {
  return new Date(Date.now() - hours * 3600_000).toISOString();
}

const MOCK_REPORTS: DailyReportDetail[] = [
  {
    report_id: "r0000000-0000-0000-0000-00000000000a",
    elder_id: MOCK_ELDERS[0].elder_id,
    report_date: localDate(0),
    version: 2,
    status: "normal",
    summary_text:
      "今日血压偏高（152/94mmHg），已同步负责医生复核。用药方面有 1 次拒服，已记录并提醒护工关注。晚间睡眠 6.5 小时，精神状态尚可。本报告为教学模拟信息，不构成医疗诊断。",
    medication_stats: { taken_reported: 2, refused: 1, missed_unconfirmed: 0, notification_failed: 0 },
    health_summary: "血压 152/94mmHg；睡眠 6.5 小时；步数 1,860",
    attention_events: [
      { level: "yellow", type: "health_referral", brief: "血压偏高，建议咨询医生", occurred_at: isoAgo(10) },
    ],
    audio_url: null,
    pushed_at: isoAgo(9),
  },
  {
    report_id: "r0000000-0000-0000-0000-00000000000b",
    elder_id: MOCK_ELDERS[0].elder_id,
    report_date: localDate(1),
    version: 1,
    status: "normal",
    summary_text:
      "全天情况平稳，用药 3 次均按时自报已服。午后散步约 30 分钟。本报告为教学模拟信息，不构成医疗诊断。",
    medication_stats: { taken_reported: 3, refused: 0, missed_unconfirmed: 0, notification_failed: 0 },
    health_summary: "血压 138/86mmHg；睡眠 7.2 小时；步数 2,540",
    attention_events: [],
    audio_url: null,
    pushed_at: isoAgo(33),
  },
  {
    report_id: "r0000000-0000-0000-0000-00000000000c",
    elder_id: MOCK_ELDERS[0].elder_id,
    report_date: localDate(2),
    version: 1,
    status: "away_notice",
    summary_text: "长者当日暂离社区，日报为暂离告知。本报告为教学模拟信息，不构成医疗诊断。",
    medication_stats: { taken_reported: 0, refused: 0, missed_unconfirmed: 0, notification_failed: 0 },
    health_summary: "",
    attention_events: [],
    audio_url: null,
    pushed_at: null,
  },
  {
    report_id: "r0000000-0000-0000-0000-00000000000d",
    elder_id: MOCK_ELDERS[1].elder_id,
    report_date: localDate(0),
    version: 1,
    status: "normal",
    summary_text:
      "今日降压药送达后未在 60 秒内确认，系统已按四次触达流程重试并通知护工。本报告为教学模拟信息，不构成医疗诊断。",
    medication_stats: { taken_reported: 1, refused: 0, missed_unconfirmed: 1, notification_failed: 0 },
    health_summary: "血压 146/90mmHg；睡眠 5.8 小时",
    attention_events: [
      { level: "red", type: "emergency_health", brief: "收缩压 185mmHg，已通知护工与家属", occurred_at: isoAgo(12) },
    ],
    audio_url: null,
    pushed_at: isoAgo(8),
  },
];

let MOCK_NOTIFICATIONS: FamilyNotification[] = [
  {
    notification_id: "n0000000-0000-0000-0000-000000000001",
    type: "emergency_health",
    payload: { elder_name: "李建国", event_brief: "收缩压 185mmHg，已通知护工，建议联系医生", report_id: "r0000000-0000-0000-0000-00000000000d" },
    channel: "inapp",
    status: "delivered",
    is_mock: true,
    retry_count: 0,
    created_at: isoAgo(12),
    sent_at: isoAgo(12),
    read_at: null,
  },
  {
    notification_id: "n0000000-0000-0000-0000-000000000002",
    type: "missed_medication",
    payload: { elder_name: "李建国", event_brief: "晚间药送达后未确认，护工已跟进", report_id: "r0000000-0000-0000-0000-00000000000d" },
    channel: "inapp",
    status: "delivered",
    is_mock: true,
    retry_count: 2,
    created_at: isoAgo(11),
    sent_at: isoAgo(11),
    read_at: null,
  },
  {
    notification_id: "n0000000-0000-0000-0000-000000000003",
    type: "report_ready",
    payload: { elder_name: "张桂芬", event_brief: "11 月日报已生成，点击查看", report_id: "r0000000-0000-0000-0000-00000000000a" },
    channel: "inapp",
    status: "delivered",
    is_mock: true,
    retry_count: 0,
    created_at: isoAgo(9),
    sent_at: isoAgo(9),
    read_at: isoAgo(8),
  },
];

// ---- 业务 fetch（mock 分支签名与真实分支一致，S-11） ----

export async function fetchBoundElders(): Promise<BoundElder[]> {
  if (USE_MOCK) return MOCK_ELDERS;
  const res = await authedFetch("/api/family/elders");
  await ensureOk(res);
  const data = (await res.json()) as Paged<BoundElder>;
  return data.items;
}

export async function fetchReports(elderId: string): Promise<DailyReportSummary[]> {
  if (USE_MOCK) {
    return MOCK_REPORTS.filter((r) => r.elder_id === elderId).map((r) => ({
      report_id: r.report_id,
      elder_id: r.elder_id,
      report_date: r.report_date,
      version: r.version,
      status: r.status,
      summary_preview: r.summary_text.slice(0, 80),
      audio_url: r.audio_url,
      pushed_at: r.pushed_at,
    }));
  }
  const res = await authedFetch(`/api/family/elders/${encodeURIComponent(elderId)}/reports`);
  await ensureOk(res);
  const data = (await res.json()) as Paged<DailyReportSummary>;
  return data.items;
}

export async function fetchReport(reportId: string): Promise<DailyReportDetail> {
  if (USE_MOCK) {
    const hit = MOCK_REPORTS.find((r) => r.report_id === reportId);
    if (!hit) throw new ApiError("invalid", "日报不存在或已删除");
    return hit;
  }
  const res = await authedFetch(`/api/family/reports/${encodeURIComponent(reportId)}`);
  await ensureOk(res);
  return (await res.json()) as DailyReportDetail;
}

export async function fetchNotifications(): Promise<FamilyNotification[]> {
  if (USE_MOCK) return MOCK_NOTIFICATIONS;
  const res = await authedFetch("/api/family/notifications?page=1&page_size=100");
  await ensureOk(res);
  const data = (await res.json()) as Paged<FamilyNotification>;
  return data.items;
}

/** N4 标记已读（幂等：重复调用返回原 read_at） */
export async function markNotificationRead(notificationId: string): Promise<string> {
  if (USE_MOCK) {
    const hit = MOCK_NOTIFICATIONS.find((n) => n.notification_id === notificationId);
    if (!hit) throw new ApiError("invalid", "通知不存在或已删除");
    hit.read_at = hit.read_at ?? new Date().toISOString();
    return hit.read_at;
  }
  const res = await authedFetch(`/api/family/notifications/${encodeURIComponent(notificationId)}/read`, {
    method: "POST",
  });
  await ensureOk(res);
  const data = (await res.json()) as { notification_id: string; read_at: string };
  return data.read_at;
}
