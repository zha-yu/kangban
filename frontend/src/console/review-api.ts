/**
 * 健康记录复核 API（task-11 F7 → Day 8 S6a 切真）。
 *
 * 字段口径唯一依据：docs/接口契约_照护台业务数据.md §3.3（R-1/R-2/R-2b/R-3/R-4）
 * 与 §3.5 `health_records` 列清单。
 * 业务规则依据：PRD FR-HLT-03/04（P0）/ AC-21（单分量红档）/ AC-22（补录追加不覆盖、
 * 原值保留可查、纠错=追加新版本）。
 *
 * - USE_MOCK 开关（Day 8 S6a → S6 翻转）：默认真实接口（R-1/R-2/R-2b/R-3/R-4），
 *   仅显式 `VITE_USE_MOCK_API=1` 回退 Mock 内存层；导出签名不变，页面层不感知。
 * - 队列 = review_status=pending 的黄/红档记录，红档置顶（f7 D2，真实模式
 *   排序由服务端 R-1 保证，页面层不再排序）；
 * - 纠错 = 生成新版本（version+1、severity 服务端重算、reviewed_by 留痕），
 *   原记录 corrected 且 superseded_by 指向新版本（f7 D3，AC-22）；
 * - review_note/superseded_reason：Q-1 补列后由服务端落列返回，Mock 层先行透传；
 * - 真实模式下 reviewerName 入参仅兼容签名——复核人身份由服务端 JWT 注入；
 *   403（非医生复核红档/越权）与 409（状态机）文案取服务端 detail。
 */

import { apiGet, apiGetWithStatus, apiPost, USE_MOCK_API } from "./api";
import {
  RULE_VERSION,
  classifyHealthRecord,
  type HealthRecordInput,
  type HealthSeverity,
  type StaffRecordSource,
} from "./health-api";

/** 契约错误文案提取（workorders-api 同构） */
async function readErrorDetail(res: Response): Promise<string> {
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  return body?.detail ?? "操作失败，请稍后重试";
}

/** 复核操作错误（真实/Mock 模式共用；页面 catch 展示 message） */
export class ReviewApiError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ReviewApiError";
  }
}

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
  review_note: string | null;
  superseded_by: string | null; // 指向新版本 record_id
  superseded_reason: SupersededBy | null;
}

export interface CorrectInput {
  fields: Partial<Pick<HealthRecordInput, "systolic" | "diastolic" | "sleep_hours" | "mood_score" | "mood_note" | "complaint">>;
  review_note: string | null;
}

// ---------------------------------------------------------------------------
// Mock 数据与行为（USE_MOCK_API=0 时不触达；后端交付前默认走这里）
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

// ---------------------------------------------------------------------------
// 真实接口（后端 app/api/health_records.py：R-1/R-2/R-2b/R-3/R-4，health_service）
// ---------------------------------------------------------------------------

/** 复核队列：pending 黄/红档，红档置顶、时间倒序（f7 D2；排序由服务端 R-1 保证） */
export async function listPendingReviews(): Promise<ReviewRecord[]> {
  if (USE_MOCK_API) {
    return delay(sortQueue(store.filter((r) => r.review_status === "pending")).map((r) => ({ ...r })));
  }
  return apiGet<ReviewRecord[]>("/api/health-records/pending-reviews");
}

/** 按 record_id 取单条（含已复核历史，用于版本链展示） */
export async function getReviewRecord(recordId: string): Promise<ReviewRecord | null> {
  if (USE_MOCK_API) {
    const found = store.find((r) => r.record_id === recordId);
    return delay(found ? { ...found } : null);
  }
  const { status, body } = await apiGetWithStatus<ReviewRecord>(
    `/api/health-records/${recordId}`,
  );
  return status === 200 ? body : null; // 404 与越权同口径 → 页面按不存在展示
}

/** 按版本链取一组记录：同长者同 recorded_at 视为同一采集批次链，旧版本在前（R-2b） */
export async function getRecordChain(recordId: string): Promise<ReviewRecord[]> {
  if (USE_MOCK_API) {
    const current = store.find((r) => r.record_id === recordId);
    if (!current) return delay([]);
    const group = store.filter(
      (r) => r.elder_id === current.elder_id && r.recorded_at === current.recorded_at,
    );
    return delay(group.sort((a, b) => a.version - b.version).map((r) => ({ ...r })));
  }
  return apiGet<ReviewRecord[]>(`/api/health-records/${recordId}/chain`);
}

/** 确认无误（仅 pending；409 状态机文案与后端一致，AC-23） */
export async function confirmReview(recordId: string, reviewerName: string, note: string | null): Promise<ReviewRecord> {
  if (USE_MOCK_API) {
    const target = store.find((r) => r.record_id === recordId);
    if (!target) throw new ReviewApiError("记录不存在");
    if (target.review_status !== "pending") throw new ReviewApiError("仅待复核记录可操作");
    target.review_status = "confirmed";
    target.reviewed_by_name = reviewerName;
    target.reviewed_at = new Date().toISOString();
    target.review_note = note;
    return delay({ ...target });
  }
  void reviewerName; // 复核人身份由服务端 JWT 注入，入参仅兼容签名
  const res = await apiPost(`/api/health-records/${recordId}/confirm`, { note });
  if (res.ok) return (await res.json()) as ReviewRecord;
  if (res.status === 404) throw new ReviewApiError("记录不存在");
  if (res.status === 409) throw new ReviewApiError("仅待复核记录可操作");
  throw new ReviewApiError(await readErrorDetail(res));
}

/** 纠错：新版本 + 原记录 corrected + superseded_by 指向（f7 D3，AC-22 原值保留） */
export async function correctReview(recordId: string, input: CorrectInput, reviewerName: string): Promise<ReviewRecord> {
  if (USE_MOCK_API) {
    const target = store.find((r) => r.record_id === recordId);
    if (!target) throw new ReviewApiError("记录不存在");
    if (target.review_status !== "pending") throw new ReviewApiError("仅待复核记录可操作");

    const changed =
      input.fields.systolic !== undefined ||
      input.fields.diastolic !== undefined ||
      input.fields.sleep_hours !== undefined ||
      input.fields.mood_score !== undefined ||
      input.fields.mood_note !== undefined ||
      input.fields.complaint !== undefined;
    if (!changed) throw new ReviewApiError("纠错至少需要修改一项数值");

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
  void reviewerName; // 复核人身份由服务端 JWT 注入，入参仅兼容签名
  // 至少改一项（契约 422）客户端先校验，避免无谓往返
  const changed =
    input.fields.systolic !== undefined ||
    input.fields.diastolic !== undefined ||
    input.fields.sleep_hours !== undefined ||
    input.fields.mood_score !== undefined ||
    input.fields.mood_note !== undefined ||
    input.fields.complaint !== undefined;
  if (!changed) throw new ReviewApiError("纠错至少需要修改一项数值");

  const res = await apiPost(`/api/health-records/${recordId}/correct`, {
    fields: input.fields,
    review_note: input.review_note,
  });
  if (res.ok) return (await res.json()) as ReviewRecord;
  if (res.status === 404) throw new ReviewApiError("记录不存在");
  if (res.status === 409) throw new ReviewApiError("仅待复核记录可操作");
  throw new ReviewApiError(await readErrorDetail(res));
}
