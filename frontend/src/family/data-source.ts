/**
 * 家属端业务类型（Day 5 · D-2 / N6；Day 8 S-11 收口后仅承载类型）。
 *
 * 契约唯一依据：docs/接口契约_家属端数据.md v1.2 已冻结（§2 字段 / §4 端点）。
 * Mock 数据与实现已于 Day 8 联调通过后按 S-11 删除（同提交登记）；
 * 本模块不承载认证域、不承载 fetch；`api.ts` 保留认证域与 5 个业务函数。
 *
 * 独立同构：禁止 import `console/**` 或 `src/pad/**`（task-05 §0）。
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
/** `risk_events.type` 五枚举（与 `app/models/medication.py:320` 的 CHECK 约束逐字一致）。
 *  契约 F2 示例里的 `emergency_health` 是示意值、不是枚举——按联调口径（计划 §13 K-1）
 *  后端原样透传五枚举，前端按此渲染，不做别名映射。 */
export type RiskEventType =
  | "red_emergency"
  | "health_alert"
  | "med_risk"
  | "sensitive"
  | "sos";

export interface AttentionEvent {
  level: "red" | "yellow";
  type: RiskEventType;
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
