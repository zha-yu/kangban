/**
 * 药单只读 Mock API（task-10 F6）。
 *
 * 字段口径唯一依据：技术设计文档 §3 `medication_plans` 表（Day 7 迁移批次，未交付）。
 * 业务规则依据：PRD FR-MED-01（P0）/ §4.5 药单权限有意决策——**医生增改停查，
 * 护工/管理员只读**；护工修改药单服务端 403 并记审计。药单为教学演示数据，
 * 不构成真实处方。
 *
 * 长者候选**复用真实 E7**（已交付，服务端范围过滤）。
 * 真实接口交付后仅替换本文件内部实现，导出签名保持不变，页面层不感知。
 */

import { listElders, type ElderListItem } from "./elders-api";

// ---------------------------------------------------------------------------
// 类型（对齐 medication_plans 表字段）
// ---------------------------------------------------------------------------

export type PlanStatus = "active" | "stopped";

export interface MedicationPlan {
  plan_id: string;
  elder_id: string;
  drug_name: string;
  dose: string; // 剂量文案，如 "5mg 一片"
  schedule_cron: string; // cron 串，页面做「每天 HH:MM」最小转换（f6 D3）
  start_date: string; // date
  end_date: string | null;
  version: number; // 修改生成新版本，历史提醒保留旧快照（AC-27）
  created_by_name: string; // 创建医生（表存 staff uuid，展示层用姓名，Mock 直接给文案）
  status: PlanStatus;
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
// Mock 数据（虚构教学演示数据，不构成真实处方）
// ---------------------------------------------------------------------------

const MOCK_PLANS: MedicationPlan[] = [
  {
    plan_id: "plan-mock-01",
    elder_id: "elder-301",
    drug_name: "硝苯地平缓释片",
    dose: "20mg 每日一片",
    schedule_cron: "0 8 * * *",
    start_date: "2026-10-20",
    end_date: null,
    version: 2,
    created_by_name: "钱医生",
    status: "active",
  },
  {
    plan_id: "plan-mock-02",
    elder_id: "elder-301",
    drug_name: "阿司匹林肠溶片",
    dose: "100mg 每日一片",
    schedule_cron: "30 7 * * *",
    start_date: "2026-10-20",
    end_date: null,
    version: 1,
    created_by_name: "钱医生",
    status: "active",
  },
  {
    plan_id: "plan-mock-03",
    elder_id: "elder-301",
    drug_name: "二甲双胍",
    dose: "0.5g 每日两次",
    schedule_cron: "0 8,18 * * *",
    start_date: "2026-09-01",
    end_date: "2026-11-30",
    version: 1,
    created_by_name: "孙医生",
    status: "active",
  },
  {
    plan_id: "plan-mock-04",
    elder_id: "elder-301",
    drug_name: "布洛芬缓释胶囊",
    dose: "0.3g 疼痛时一次",
    schedule_cron: "0 12 * * *",
    start_date: "2026-10-05",
    end_date: "2026-10-15",
    version: 1,
    created_by_name: "钱医生",
    status: "stopped",
  },
  {
    plan_id: "plan-mock-05",
    elder_id: "elder-305",
    drug_name: "氨氯地平",
    dose: "5mg 每日一片",
    schedule_cron: "0 8 * * *",
    start_date: "2026-11-01",
    end_date: null,
    version: 3,
    created_by_name: "孙医生",
    status: "active",
  },
  {
    plan_id: "plan-mock-06",
    elder_id: "elder-305",
    drug_name: "单硝酸异山梨酯",
    dose: "40mg 每日一片",
    schedule_cron: "0 9 * * 1",
    start_date: "2026-11-01",
    end_date: null,
    version: 1,
    created_by_name: "孙医生",
    status: "active",
  },
  {
    plan_id: "plan-mock-07",
    elder_id: "elder-303",
    drug_name: "美托洛尔",
    dose: "25mg 每日一片",
    schedule_cron: "0 8 * * *",
    start_date: "2026-10-10",
    end_date: null,
    version: 1,
    created_by_name: "钱医生",
    status: "active",
  },
];

let store: MedicationPlan[] = MOCK_PLANS.map((p) => ({ ...p }));

// ---------------------------------------------------------------------------
// Mock 行为（真实接口交付后仅替换本节实现）
// ---------------------------------------------------------------------------

const MOCK_DELAY_MS = 200;

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), MOCK_DELAY_MS));
}

/** 按长者查药单（只读；真实接口由服务端范围过滤 + 403 兜底） */
export async function listPlansByElder(elderId: string): Promise<MedicationPlan[]> {
  return delay(
    store
      .filter((p) => p.elder_id === elderId)
      .map((p) => ({ ...p })),
  );
}

/** 负责长者候选（透传真实 E7） */
export async function listResponsibleElders(): Promise<ElderListItem[]> {
  const res = await listElders({ page: 1, pageSize: 100 });
  return res.items;
}

// ---------------------------------------------------------------------------
// 医生写操作（task-12 F8，§4.5：医生增改停查；与护工只读页共享同一 store，
// 护工页天然可见医生改动。真实接口交付后仅替换本节实现）
// ---------------------------------------------------------------------------

export interface PlanWriteInput {
  elder_id: string;
  drug_name: string;
  dose: string;
  schedule_cron: string;
  start_date: string;
  end_date: string | null;
}

let planSeq = store.length + 1;

function assertFields(input: PlanWriteInput): void {
  if (!input.elder_id) throw new Error("请选择长者");
  if (!input.drug_name.trim()) throw new Error("药品名称必填");
  if (!input.dose.trim()) throw new Error("剂量必填");
  if (!input.schedule_cron.trim()) throw new Error("服药时间（cron）必填");
  if (!input.start_date) throw new Error("开始日期必填");
}

/** 新增药单：version=1、active、created_by=当前医生 */
export async function createPlan(input: PlanWriteInput, doctorName: string): Promise<MedicationPlan> {
  assertFields(input);
  const n = planSeq++;
  const plan: MedicationPlan = {
    plan_id: `plan-mock-${String(n).padStart(2, "0")}`,
    elder_id: input.elder_id,
    drug_name: input.drug_name.trim(),
    dose: input.dose.trim(),
    schedule_cron: input.schedule_cron.trim(),
    start_date: input.start_date,
    end_date: input.end_date,
    version: 1,
    created_by_name: doctorName,
    status: "active",
  };
  store.push(plan);
  return delay({ ...plan });
}

/** 修改药单：同 plan_id 原地 version+1（AC-27：历史提醒保留提交时快照，不受影响） */
export async function updatePlan(planId: string, input: PlanWriteInput, doctorName: string): Promise<MedicationPlan> {
  assertFields(input);
  const target = store.find((p) => p.plan_id === planId);
  if (!target) throw new Error("药单不存在");
  if (target.status === "stopped") throw new Error("已停用药单不可修改，请重新新增");
  target.drug_name = input.drug_name.trim();
  target.dose = input.dose.trim();
  target.schedule_cron = input.schedule_cron.trim();
  target.start_date = input.start_date;
  target.end_date = input.end_date;
  target.version += 1;
  target.created_by_name = doctorName;
  return delay({ ...target });
}

/** 停用药单：status→stopped；无删除语义，不可恢复（教学级，f8 D4 留痕） */
export async function stopPlan(planId: string, doctorName: string): Promise<MedicationPlan> {
  const target = store.find((p) => p.plan_id === planId);
  if (!target) throw new Error("药单不存在");
  if (target.status === "stopped") throw new Error("该药单已停用");
  target.status = "stopped";
  target.version += 1;
  target.created_by_name = doctorName;
  return delay({ ...target });
}
