/**
 * 健康记录复核 Mock API（task-11 F7）。
 *
 * 字段口径唯一依据：技术设计文档 §3 `health_records` 表（Day 8 迁移批次，未交付）。
 * 业务规则依据：PRD FR-HLT-03/04（P0）/ AC-21（单分量红档）/ AC-22（补录追加不覆盖、
 * 原值保留可查）。
 *
 * - 队列 = review_status=pending 的黄/红档记录，红档置顶（f7 D2）；
 * - 纠错 = 生成新版本（version+1、severity 重算、reviewed_by 留痕），
 *   原记录 corrected 且 superseded_by 指向新版本（f7 D3）；
 * - review_note 为 Mock 自裁展示字段（表无此列，f7 D4 留痕）。
 * 真实接口交付后仅替换本文件内部实现，导出签名保持不变，页面层不感知。
 */

import {
  RULE_VERSION,
  classifyHealthRecord,
  type HealthRecordInput,
  type HealthSeverity,
  type StaffRecordSource,
} from "./health-api";

export type ReviewStatus = "pending" | "confirmed" | "corrected";
export type SupersededBy = "review_correction" | "backfill";

export interface ReviewRecord {
  record_id: string;
  elder_id: string;
  elder_name: string; // 真实接口 JOIN 回填；Mock 冗余
  room_no: string | null; // 展示辅助
  bed_no: string | null;
  source: StaffRecordSource;
  operator_name: string; // 代填护工（operator_id 的展示文案）
  recorded_at: string; // ISO UTC
  systolic: number | null;
  diastolic: number | null;
  sleep_hours: number | null;
  mood_score: number | null;
  mood_note: string | null;
  complaint: string | null;
  severity: HealthSeverity;
  rule_version: string;
  version: number;
  review_status: ReviewStatus;
  reviewed_by_name: string | null;
  reviewed_at: string | null;
  review_note: string | null; // Mock 自裁字段（f7 D4）
  superseded_by: string | null; // 指向新版本 record_id
  superseded_reason: SupersededBy | null;
}

export interface CorrectInput {
  fields: Partial<Pick<HealthRecordInput, "systolic" | "diastolic" | "sleep_hours" | "mood_score" | "mood_note" | "complaint">>;
  review_note: string | null;
}

// ---------------------------------------------------------------------------
// Mock 数据（虚构教学演示数据）
// ---------------------------------------------------------------------------

let store: ReviewRecord[] = [
  {
    record_id: "rv-mock-001",
    elder_id: "elder-301",
    elder_name: "演示·周慧兰",
    room_no: "301",
    bed_no: "1",
    source: "staff_scheduled",
    operator_name: "护工·刘芳",
    recorded_at: "2026-11-05T11:35:00Z",
    systolic: 185,
    diastolic: 110,
    sleep_hours: 6,
    mood_score: 3,
    mood_note: null,
    complaint: null,
    severity: "red",
    rule_version: RULE_VERSION,
    version: 1,
    review_status: "pending",
    reviewed_by_name: null,
    reviewed_at: null,
    review_note: null,
    superseded_by: null,
    superseded_reason: null,
  },
  {
    record_id: "rv-mock-002",
    elder_id: "elder-305",
    elder_name: "演示·吴国栋",
    room_no: "305",
    bed_no: "2",
    source: "staff_active",
    operator_name: "护工·刘芳",
    recorded_at: "2026-11-05T11:40:00Z",
    systolic: 135,
    diastolic: 88,
    sleep_hours: 4,
    mood_score: 3,
    mood_note: null,
    complaint: null,
    severity: "yellow",
    rule_version: RULE_VERSION,
    version: 1,
    review_status: "pending",
    reviewed_by_name: null,
    reviewed_at: null,
    review_note: null,
    superseded_by: null,
    superseded_reason: null,
  },
  {
    record_id: "rv-mock-003",
    elder_id: "elder-301",
    elder_name: "演示·周慧兰",
    room_no: "301",
    bed_no: "1",
    source: "staff_scheduled",
    operator_name: "护工·王敏",
    recorded_at: "2026-11-04T11:30:00Z",
    systolic: 130,
    diastolic: 85,
    sleep_hours: 7,
    mood_score: 4,
    mood_note: null,
    complaint: null,
    severity: "red", // 误输：收缩压实际 130，被输成 180
    rule_version: RULE_VERSION,
    version: 1,
    review_status: "corrected",
    reviewed_by_name: "演示·钱医生",
    reviewed_at: "2026-11-04T12:10:00Z",
    review_note: "收缩压误输为 180，实际 130（现场二次测量复核）",
    superseded_by: "rv-mock-004",
    superseded_reason: "review_correction",
  },
  {
    record_id: "rv-mock-004",
    elder_id: "elder-301",
    elder_name: "演示·周慧兰",
    room_no: "301",
    bed_no: "1",
    source: "staff_scheduled",
    operator_name: "护工·王敏",
    recorded_at: "2026-11-04T11:30:00Z",
    systolic: 130,
    diastolic: 85,
    sleep_hours: 7,
    mood_score: 4,
    mood_note: null,
    complaint: null,
    severity: "green",
    rule_version: RULE_VERSION,
    version: 2,
    review_status: "confirmed",
    reviewed_by_name: "演示·钱医生",
    reviewed_at: "2026-11-04T12:10:00Z",
    review_note: "纠错版本，与现场二次测量一致",
    superseded_by: null,
    superseded_reason: null,
  },
];

let recordSeq = 5;

// ---------------------------------------------------------------------------
// Mock 行为（真实接口交付后仅替换本节实现）
// ---------------------------------------------------------------------------

const MOCK_DELAY_MS = 200;

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), MOCK_DELAY_MS));
}

function sortQueue(items: ReviewRecord[]): ReviewRecord[] {
  const rank: Record<HealthSeverity, number> = { red: 0, yellow: 1, green: 2 };
  return [...items].sort(
    (a, b) => rank[a.severity] - rank[b.severity] || b.recorded_at.localeCompare(a.recorded_at),
  );
}

/** 复核队列：pending 黄/红档，红档置顶、时间倒序（f7 D2） */
export async function listPendingReviews(): Promise<ReviewRecord[]> {
  return delay(sortQueue(store.filter((r) => r.review_status === "pending")).map((r) => ({ ...r })));
}

/** 按 record_id 取单条（含已复核历史，用于版本链展示） */
export async function getReviewRecord(recordId: string): Promise<ReviewRecord | null> {
  const found = store.find((r) => r.record_id === recordId);
  return delay(found ? { ...found } : null);
}

/** 按版本链取一组记录：同长者同 recorded_at 视为同一采集批次链，旧版本在前 */
export async function getRecordChain(recordId: string): Promise<ReviewRecord[]> {
  const current = store.find((r) => r.record_id === recordId);
  if (!current) return delay([]);
  const group = store.filter(
    (r) => r.elder_id === current.elder_id && r.recorded_at === current.recorded_at,
  );
  return delay(group.sort((a, b) => a.version - b.version).map((r) => ({ ...r })));
}

/** 确认无误（仅 pending） */
export async function confirmReview(recordId: string, reviewerName: string, note: string | null): Promise<ReviewRecord> {
  const target = store.find((r) => r.record_id === recordId);
  if (!target) throw new Error("记录不存在");
  if (target.review_status !== "pending") throw new Error("仅待复核记录可操作");
  target.review_status = "confirmed";
  target.reviewed_by_name = reviewerName;
  target.reviewed_at = new Date().toISOString();
  target.review_note = note;
  return delay({ ...target });
}

/** 纠错：新版本 + 原记录 corrected + superseded_by 指向（f7 D3，AC-22 原值保留） */
export async function correctReview(recordId: string, input: CorrectInput, reviewerName: string): Promise<ReviewRecord> {
  const target = store.find((r) => r.record_id === recordId);
  if (!target) throw new Error("记录不存在");
  if (target.review_status !== "pending") throw new Error("仅待复核记录可操作");

  const changed =
    input.fields.systolic !== undefined ||
    input.fields.diastolic !== undefined ||
    input.fields.sleep_hours !== undefined ||
    input.fields.mood_score !== undefined ||
    input.fields.mood_note !== undefined ||
    input.fields.complaint !== undefined;
  if (!changed) throw new Error("纠错至少需要修改一项数值");

  const n = recordSeq++;
  const next: ReviewRecord = {
    ...target,
    record_id: `rv-mock-${String(n).padStart(3, "0")}`,
    ...input.fields,
    severity: classifyHealthRecord({
      elder_id: target.elder_id,
      source: target.source,
      systolic: input.fields.systolic ?? target.systolic,
      diastolic: input.fields.diastolic ?? target.diastolic,
      sleep_hours: input.fields.sleep_hours ?? target.sleep_hours,
      mood_score: input.fields.mood_score ?? target.mood_score,
      mood_note: input.fields.mood_note ?? target.mood_note,
      complaint: input.fields.complaint ?? target.complaint,
    }).severity,
    rule_version: RULE_VERSION,
    version: target.version + 1,
    review_status: "confirmed",
    reviewed_by_name: reviewerName,
    reviewed_at: new Date().toISOString(),
    review_note: input.review_note ?? "纠错版本",
    superseded_by: null,
    superseded_reason: null,
  };
  target.review_status = "corrected";
  target.reviewed_by_name = reviewerName;
  target.reviewed_at = new Date().toISOString();
  if (input.review_note) target.review_note = input.review_note;
  target.superseded_by = next.record_id;
  target.superseded_reason = "review_correction";
  store.push(next);
  return delay({ ...next });
}
