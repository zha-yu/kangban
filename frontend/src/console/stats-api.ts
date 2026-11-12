/**
 * 统计与审计 API（Day 9 · C6 · S5）。
 *
 * 统计：GET /api/stats/weekly（FR-ALT-05 三指标，admin-only，已交付 F13）——
 * week_start（UTC ISO）/ red_events_count / avg_ack_minutes / missed_by_building。
 *
 * 审计日志（消费 A4）：**A4 端点尚未定稿交付**，路径集中为 `AUDIT_ENDPOINTS` 常量，
 * A4 交接后只改这一处；404 时页面显示「A4 未交付」。预期路径：
 * GET /api/audit-logs（时间/操作人筛选）＋ GET /api/audit-logs/export.csv（CSV 导出）。
 * 审计行形态未定稿，字段可选、未知字段透传展示（AGENTS §4 禁区 9：不臆造）。
 */

import { apiGet, apiGetRaw } from "./api";

// ---------------------------------------------------------------------------
// 周统计（已交付 F13，路径与 app/api/stats.py 一致）
// ---------------------------------------------------------------------------

export interface WeeklyStats {
  week_start: string;
  red_events_count: number;
  avg_ack_minutes: number | null;
  missed_by_building: Record<string, number>;
}

/** 本周运营统计（FR-ALT-05；Asia/Shanghai 自然周，落库 UTC） */
export function getWeeklyStats(): Promise<WeeklyStats> {
  return apiGet<WeeklyStats>("/api/stats/weekly");
}

// ---------------------------------------------------------------------------
// 审计日志（A4 预期路径，未定稿集中常量）
// ---------------------------------------------------------------------------

export const AUDIT_ENDPOINTS = {
  logs: "/api/audit-logs",
  exportCsv: "/api/audit-logs/export.csv",
} as const;

/** 审计日志筛选（A4 计划口径：按时间 / 操作人；动作类型一并预留） */
export interface AuditLogFilter {
  /** yyyy-mm-dd（含）起 */
  start_date?: string;
  /** yyyy-mm-dd（含）止 */
  end_date?: string;
  /** 操作人姓名/ID 模糊匹配（A4 定稿后对齐） */
  actor?: string;
}

/** 审计行（A4 未定稿：可选字段 + 未知透传） */
export interface AuditLogItem {
  id?: string;
  action?: string;
  actor_id?: string;
  actor_name?: string;
  created_at?: string;
  detail?: string;
  trace_id?: string;
  [key: string]: unknown;
}

export interface AuditLogPage {
  items: AuditLogItem[];
  total?: number;
}

export type AuditErrorKind =
  | "not_delivered" // 404：A4 端点未交付
  | "forbidden" // 403：越权
  | "server";

export class AuditApiError extends Error {
  constructor(
    public readonly kind: AuditErrorKind,
    message: string,
  ) {
    super(message);
    this.name = "AuditApiError";
  }
}

async function readErrorDetail(res: Response): Promise<string> {
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  return body?.detail ?? "操作失败，请稍后重试";
}

function buildAuditQuery(filter: AuditLogFilter): string {
  const params = new URLSearchParams();
  if (filter.start_date) params.set("start_date", filter.start_date);
  if (filter.end_date) params.set("end_date", filter.end_date);
  if (filter.actor) params.set("actor", filter.actor);
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

async function assertAuditOk(res: Response): Promise<void> {
  if (res.ok) return;
  const detail = await readErrorDetail(res);
  if (res.status === 404) {
    throw new AuditApiError("not_delivered", "A4 未交付：审计端点尚未部署");
  }
  if (res.status === 403) {
    throw new AuditApiError("forbidden", "无权限查看审计日志（仅管理员）");
  }
  throw new AuditApiError("server", detail);
}

/** 审计日志列表（时间/操作人筛选；A4 未交付时抛 not_delivered） */
export async function listAuditLogs(filter: AuditLogFilter): Promise<AuditLogPage> {
  const res = await apiGet<AuditLogPage>(`${AUDIT_ENDPOINTS.logs}${buildAuditQuery(filter)}`);
  return res;
}

/** 导出审计 CSV（FR-ADM-05）：成功则触发浏览器下载，内容与审计表一致 */
export async function exportAuditCsv(filter: AuditLogFilter): Promise<void> {
  const res = await apiGetRaw(`${AUDIT_ENDPOINTS.exportCsv}${buildAuditQuery(filter)}`);
  await assertAuditOk(res);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    const today = new Date().toISOString().slice(0, 10);
    a.href = url;
    a.download = `audit-logs-${today}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

// ---------------------------------------------------------------------------
// 展示工具
// ---------------------------------------------------------------------------

const timeFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatAuditTime(iso: string | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : timeFmt.format(d);
}

/** 审计单元格兜底：已知字段缺失时展示原始 JSON（形态未定稿，不臆造） */
export function auditCell(value: unknown): string {
  if (value === undefined || value === null || value === "") return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}
