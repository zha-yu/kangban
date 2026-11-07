/**
 * 家属端业务数据源（Day 5 · D-2 / N6）。
 *
 * 契约唯一依据：docs/接口契约_家属端数据.md v1.0 草案（§2 字段 / §4 端点 / §6 自裁）。
 * 本模块只承载业务类型、Mock 数据与 Mock 实现，不承载认证域；
 * `api.ts` 保留认证域、唯一 `USE_MOCK` 开关、类型导出与 5 个业务函数签名。
 *
 * 独立同构：禁止 import `console/**` 或 `src/pad/**`（task-05 §0）。
 * Mock 数据结构与真实响应逐字段一致（S-11）；真实分支待契约冻结评审（X3）后定稿。
 */

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

/** F0 外壳：契约 §4 F0 只返回 `{items}`（无分页字段） */
export interface ListItems<T> {
  items: T[];
}

/** F1/N1 分页外壳（契约 §4：`{items,total,page,page_size}`） */
export interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

// ---- Mock 数据（S-11：结构与真实接口响应逐字段一致；虚构教学数据） ----

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

const MOCK_NOTIFICATIONS: FamilyNotification[] = [
  {
    notification_id: "n0000000-0000-0000-0000-000000000001",
    type: "emergency_health",
    payload: {
      elder_name: "李建国",
      event_brief: "收缩压 185mmHg，已通知护工，建议联系医生",
      report_id: "r0000000-0000-0000-0000-00000000000d",
    },
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
    payload: {
      elder_name: "李建国",
      event_brief: "晚间药送达后未确认，护工已跟进",
      report_id: "r0000000-0000-0000-0000-00000000000d",
    },
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
    payload: {
      elder_name: "张桂芬",
      event_brief: "11 月日报已生成，点击查看",
      report_id: "r0000000-0000-0000-0000-00000000000a",
    },
    channel: "inapp",
    status: "delivered",
    is_mock: true,
    retry_count: 0,
    created_at: isoAgo(9),
    sent_at: isoAgo(9),
    read_at: isoAgo(8),
  },
];

// ---- Mock 实现（与真实分支同签名；仅供 api.ts 的 USE_MOCK 分支调用） ----

export function mockBoundElders(): BoundElder[] {
  return MOCK_ELDERS;
}

export function mockReports(elderId: string): DailyReportSummary[] {
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

export function mockReportDetail(reportId: string): DailyReportDetail | null {
  return MOCK_REPORTS.find((r) => r.report_id === reportId) ?? null;
}

export function mockNotifications(): FamilyNotification[] {
  return MOCK_NOTIFICATIONS;
}

/** N4 Mock 已读（幂等：已有 read_at 原样返回） */
export function mockMarkNotificationRead(notificationId: string): string | null {
  const hit = MOCK_NOTIFICATIONS.find((n) => n.notification_id === notificationId);
  if (!hit) return null;
  hit.read_at = hit.read_at ?? new Date().toISOString();
  return hit.read_at;
}
