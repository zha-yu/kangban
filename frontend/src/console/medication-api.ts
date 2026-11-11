/**
 * 药单管理 API（Day 8 dev-A H+1，2026-11-11）。
 *
 * 2026-11-11：后端 `GET/POST/PUT /api/medication-plans*` 已交付
 * （app/api/medication.py + app/services/medication_plan_service.py + 迁移 0019），
 * 本文件已从 Mock 层切换为真实接口；**导出签名保持不变**，页面层无感知。
 *
 * 字段口径唯一依据：后端 app/api/medication.py 的 PlanOut 模型
 * （对齐 MedicationPlan 类型，新增 created_at 字段）。
 *
 * 权限口径（后端双重门禁）：
 * - API 层放行所有 staff 角色（require_roles("doctor","care_worker","admin")）；
 * - Service 层读 scope 过滤：admin 全量；doctor/care_worker 只看自己负责的长者；
 * - Service 层写双重门禁：role=doctor + elder_staff_assignments relation='doctor' scope；
 *   越权写返回 403，越权读 list 返回空集合 / get 返回 404（不暴露存在性）。
 *
 * 版本管理（G1 裁决，迁移 0019）：plan 唯一键升级为三列
 * UNIQUE(plan_id, scheduled_at, plan_version)——改药单后 version+1，
 * 新事件自动带新版本号，不再触发 MedicationConflict。
 * 历史事件 drug_snapshot 是独立 JSONB 列，plan 改版不影响（AC-27）。
 */

import { apiGet, apiPost, apiPut } from "./api";
import { listElders, type ElderListItem } from "./elders-api";

// ---------------------------------------------------------------------------
// 类型（对齐后端 PlanOut / PlanCreateIn / PlanUpdateIn）
// ---------------------------------------------------------------------------

export type PlanStatus = "active" | "stopped";

export interface MedicationPlan {
  plan_id: string;
  elder_id: string;
  drug_name: string;
  dose: string;
  schedule_cron: string;
  start_date: string; // date (YYYY-MM-DD)
  end_date: string | null;
  version: number;
  created_by_name: string | null; // 后端 JOIN staff_accounts 返回，理论上永远有值
  status: PlanStatus;
  created_at: string; // ISO datetime（后端新增，Mock 时代未定义；多字段 TS 自动忽略）
}

// ---------------------------------------------------------------------------
// 展示辅助
// ---------------------------------------------------------------------------

export const PLAN_STATUS_LABELS: Record<PlanStatus, string> = {
  active: "用药中",
  stopped: "已停用",
};

/** 最小 cron 转换：仅支持「每天 HH:MM」（分 时 * * *），其余原样返回（f6 D3） */
export function cronToText(cron: string): string {
  const m = cron.match(/^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*$/);
  if (!m) return cron;
  const minute = m[1].padStart(2, "0");
  const hour = m[2].padStart(2, "0");
  return `每天 ${hour}:${minute}`;
}

// ---------------------------------------------------------------------------
// 错误处理（复用 workorders-api.ts 模式：从 Response.detail 读后端错误文案）
// ---------------------------------------------------------------------------

export class PlanApiError extends Error {
  constructor(
    public readonly code:
      | "forbidden" // 403：Service 层双重门禁不通过（role_not_doctor / not_attending_doctor_for_elder）
      | "not_found" // 404：plan 不存在或 scope 外
      | "stopped" // 400：已停用药单不可修改/停用
      | "invalid_cron" // 422：cron 表达式不合法
      | "validation" // 其余 4xx（参数校验失败）
      | "unknown", // 5xx 等其余
    message: string,
  ) {
    super(message);
    this.name = "PlanApiError";
  }
}

async function readErrorDetail(res: Response): Promise<string> {
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  return body?.detail ?? "操作失败";
}

function classifyError(status: number, detail: string): PlanApiError {
  if (status === 403) return new PlanApiError("forbidden", detail);
  if (status === 404) return new PlanApiError("not_found", detail);
  if (status === 400 && detail.includes("停用")) return new PlanApiError("stopped", detail);
  if (status === 422) return new PlanApiError("invalid_cron", detail);
  if (status < 500) return new PlanApiError("validation", detail);
  return new PlanApiError("unknown", detail);
}

// ---------------------------------------------------------------------------
// 只读接口（后端 Service scope 过滤 + 越权读审计）
// ---------------------------------------------------------------------------

/** 按长者查药单（后端 scope 过滤；越权读返回空集合） */
export async function listPlansByElder(elderId: string): Promise<MedicationPlan[]> {
  return apiGet<MedicationPlan[]>(`/api/medication-plans?elder_id=${encodeURIComponent(elderId)}`);
}

/** 负责长者候选（透传真实 elders-api） */
export async function listResponsibleElders(): Promise<ElderListItem[]> {
  const res = await listElders({ page: 1, pageSize: 100 });
  return res.items;
}

// ---------------------------------------------------------------------------
// 医生写操作（后端 Service 双重门禁 + 版本管理 + 审计）
// ---------------------------------------------------------------------------

export interface PlanWriteInput {
  elder_id: string;
  drug_name: string;
  dose: string;
  schedule_cron: string;
  start_date: string;
  end_date: string | null;
}

/** 新增药单：POST /api/medication-plans → 201（后端 version=1, status=active） */
export async function createPlan(
  input: PlanWriteInput,
  _doctorName: string,
): Promise<MedicationPlan> {
  // doctorName 参数仅兼容 Mock 时代的导出签名；后端从 JWT 注入 created_by，前端传值丢弃
  const res = await apiPost("/api/medication-plans", {
    elder_id: input.elder_id,
    drug_name: input.drug_name.trim(),
    dose: input.dose.trim(),
    schedule_cron: input.schedule_cron.trim(),
    start_date: input.start_date,
    end_date: input.end_date ?? null,
  });
  if (!res.ok) {
    const detail = await readErrorDetail(res);
    throw classifyError(res.status, detail);
  }
  return (await res.json()) as MedicationPlan;
}

/** 修改药单：PUT /api/medication-plans/{planId}（后端 version+1；停用不可改） */
export async function updatePlan(
  planId: string,
  input: PlanWriteInput,
  _doctorName: string,
): Promise<MedicationPlan> {
  const res = await apiPut(`/api/medication-plans/${encodeURIComponent(planId)}`, {
    // elder_id 由 plan 表原值锁定（plan_id 已定位），后端 PlanUpdateIn 接收但 Service 层忽略
    elder_id: input.elder_id,
    drug_name: input.drug_name.trim(),
    dose: input.dose.trim(),
    schedule_cron: input.schedule_cron.trim(),
    start_date: input.start_date,
    end_date: input.end_date ?? null,
  });
  if (!res.ok) {
    const detail = await readErrorDetail(res);
    throw classifyError(res.status, detail);
  }
  return (await res.json()) as MedicationPlan;
}

/** 停用药单：POST /api/medication-plans/{planId}/stop（后端 version+1；无删除语义） */
export async function stopPlan(planId: string, _doctorName: string): Promise<MedicationPlan> {
  const res = await apiPost(`/api/medication-plans/${encodeURIComponent(planId)}/stop`, {});
  if (!res.ok) {
    const detail = await readErrorDetail(res);
    throw classifyError(res.status, detail);
  }
  return (await res.json()) as MedicationPlan;
}
