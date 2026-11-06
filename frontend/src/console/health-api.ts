/**
 * 健康采集代填 Mock API（task-09 F5）。
 *
 * 字段口径唯一依据：技术设计文档 §3 `health_records` 表（Day 8 迁移批次，未交付）。
 * 业务规则依据：PRD FR-HLT-06（P1）/ §4.6 分级阈值 / AC-32（代填边界）。
 *
 * - 提交走 Mock：分级按阈值常量模拟（f5 D3），正式分级由服务端规则引擎写入；
 * - 负责长者候选**复用真实 E7**（已交付，服务端范围过滤保证只出现负责长者，AC-32）。
 * 真实接口交付后仅替换本文件内部实现，导出签名保持不变，页面层不感知。
 */

import { listElders, type ElderListItem } from "./elders-api";

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

/** source 按技术文档 §3.4 四值枚举取护工两值（f5 D2：AC-32 行文 staff_report 不采用） */
export type StaffRecordSource = "staff_scheduled" | "staff_active";

export type HealthSeverity = "green" | "yellow" | "red";

export const SOURCE_LABELS: Record<StaffRecordSource, string> = {
  staff_scheduled: "定时代填（采集窗口内补录）",
  staff_active: "主动代填（非窗口时段）",
};

export interface HealthRecordInput {
  elder_id: string;
  source: StaffRecordSource;
  systolic: number | null; // mmHg，可空（单分量可缺）
  diastolic: number | null;
  sleep_hours: number | null; // 0–24，>24 无效
  mood_score: number | null; // 1–5（1=很差 5=很好）
  mood_note: string | null;
  complaint: string | null; // 主诉原文
}

export interface HealthRecordResult {
  record_id: string;
  elder_id: string;
  source: StaffRecordSource;
  severity: HealthSeverity;
  /** 分级规则版本（技术文档：保存 rule_version 可追溯） */
  rule_version: string;
  /** 后续任务提示（§4.6：红→紧急工单+通知；黄→人工复核；绿→正常入库） */
  follow_up: string;
  recorded_at: string; // ISO UTC
}

// ---------------------------------------------------------------------------
// 阈值常量（PRD §4.6，硬编码不得调整；AGENTS §7）
// ---------------------------------------------------------------------------

export const BP_SYS_RED = 180; // 收缩压 ≥180 红
export const BP_DIA_RED = 120; // 舒张压 ≥120 红（单分量即红）
const BP_SYS_YELLOW_LOW = 90; // 收缩压 <90 黄
const BP_DIA_YELLOW_LOW = 60; // 舒张压 <60 黄
const BP_SYS_YELLOW_HIGH = 179; // 收缩压 140–179 黄
const BP_DIA_YELLOW_HIGH = 119; // 舒张压 90–119 黄
const SLEEP_YELLOW = 5; // 睡眠 <5h 黄
const MOOD_YELLOW = 2; // mood_score ≤2 且未命中急症 黄

export const RULE_VERSION = "v1.0";

// ---------------------------------------------------------------------------
// 分级（Mock 模拟服务端规则引擎；正式分级以后端写入为准）
// ---------------------------------------------------------------------------

export function classifyHealthRecord(input: HealthRecordInput): { severity: HealthSeverity; follow_up: string } {
  const { systolic, diastolic, sleep_hours, mood_score, complaint } = input;
  const missing = systolic === null && diastolic === null && sleep_hours === null && mood_score === null && !complaint;

  // 红档：单分量达红即判红（AC-21）
  if ((systolic !== null && systolic >= BP_SYS_RED) || (diastolic !== null && diastolic >= BP_DIA_RED)) {
    return { severity: "red", follow_up: "已生成紧急工单并同步通知管理员（Mock 模拟）" };
  }

  // 黄档任一命中
  const bpYellow =
    (systolic !== null && (systolic >= 140 && systolic <= BP_SYS_YELLOW_HIGH || systolic < BP_SYS_YELLOW_LOW)) ||
    (diastolic !== null && (diastolic >= 90 && diastolic <= BP_DIA_YELLOW_HIGH || diastolic < BP_DIA_YELLOW_LOW));
  if (
    missing ||
    bpYellow ||
    (sleep_hours !== null && sleep_hours < SLEEP_YELLOW) ||
    (mood_score !== null && mood_score <= MOOD_YELLOW) ||
    !!complaint
  ) {
    return { severity: "yellow", follow_up: "已生成黄色人工复核任务（Mock 模拟）" };
  }

  return { severity: "green", follow_up: "记录已入库，纳入当日日报（Mock 模拟）" };
}

let recordSeq = 1;

/** 代填提交（Mock：分级 + 后续任务提示；补录追加不覆盖，AC-22） */
export async function submitHealthRecord(input: HealthRecordInput): Promise<HealthRecordResult> {
  const { severity, follow_up } = classifyHealthRecord(input);
  const n = recordSeq++;
  return {
    record_id: `hr-mock-${String(n).padStart(3, "0")}`,
    elder_id: input.elder_id,
    source: input.source,
    severity,
    rule_version: RULE_VERSION,
    follow_up,
    recorded_at: new Date().toISOString(),
  };
}

/** 负责长者候选（透传真实 E7；非负责长者天然不在候选集，AC-32） */
export async function listResponsibleElders(): Promise<ElderListItem[]> {
  const res = await listElders({ page: 1, pageSize: 100, status: "active" });
  return res.items;
}
