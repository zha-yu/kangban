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
