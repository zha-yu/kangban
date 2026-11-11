/**
 * 任务面板类型定义与教学模拟数据（接口待接，PRD §4.5/§4.6/§3.2）。
 *
 * 本文件在第 2 批创建后因未提交而丢失，2026-11-06 按 4 个页面
 * （MedicationCard / HealthCheckup / IdentityConfirm / DemoSwitcher）的
 * 实际用法与 PRD §4.6「5 问 5 字段」定义重建；重建已获主理人授权，
 * 登记于 docs/team_tasks/day04/a0-联调遗留问题修复.md。
 *
 * 本模块只提供类型与 mock 数据，不发起任何请求；分级、写库、
 * 业务事实判定一律在后端（前端不做健康分级，前端页面方案 §4.4）。
 */

/** 任务面板类型：none = 无任务（主对话），其余对应四类任务面板 */
export type TaskKind = "none" | "medication" | "health" | "identity" | "sos";

// ---------------------------------------------------------------------------
// 用药提醒快照（PRD §4.5：药品快照保留历史提醒旧值，不随药单修改变化）
// ---------------------------------------------------------------------------

export interface MedicationSnapshot {
  /** 药品名称 */
  drugName: string;
  /** 剂量 */
  dose: string;
  /** 计划时间（展示用，如「08:00」） */
  scheduledAt: string;
  /** 服用方式 */
  usage: string;
  /** 本次触达序号（T0/+5/+10/+15，FR-MED-06） */
  attemptNo: number;
  /** 确认窗口秒数（每次触达等 60s，PRD §4.5） */
  windowSeconds: number;
  /** 快照生成时间（展示用） */
  snapshotAt: string;
}

/** 用药提醒 mock 数据（布局验收用，接口待接） */
export const MOCK_MEDICATION: MedicationSnapshot = {
  drugName: "苯磺酸氨氯地平片",
  dose: "5mg × 1 片",
  scheduledAt: "08:00",
  usage: "口服，温水送服",
  attemptNo: 1,
  windowSeconds: 60,
  snapshotAt: "08:00 提醒",
};

// ---------------------------------------------------------------------------
// 健康采集 5 问（PRD §4.6：5 个问题对应 5 个结构化字段，每问等 60 秒）
// ---------------------------------------------------------------------------

/** 5 问对应的 5 个结构化字段（PRD §4.6 表格） */
export type HealthField =
  | "systolic"
  | "diastolic"
  | "sleep_hours"
  | "mood_score"
  | "complaint";

/** 文字按钮兜底选项（麦克风权限被拒时仍能完成采集，FR-UI-03） */
export interface HealthOption {
  value: string;
  label: string;
}

export interface HealthQuestion {
  key: HealthField;
  /** 问句（大字幕标题） */
  label: string;
  /** 次级说明 */
  hint: string;
  /** number = 数字输入兜底；choice = 选项按钮兜底 */
  kind: "number" | "choice";
  /** 数字题允许小数（仅 sleep_hours——后端 sleep_hours 为 float 0–24；血压为整数） */
  decimal?: boolean;
  /** 采集中数字单位（拼进答案展示值） */
  unit?: string;
  /** choice 型的选项集 */
  options?: HealthOption[];
}

export const HEALTH_QUESTIONS: HealthQuestion[] = [
  {
    key: "systolic",
    label: "今天测量的收缩压是多少？",
    hint: "如果今天还没测，可以说「没测」",
    kind: "number",
    unit: "mmHg",
  },
  {
    key: "diastolic",
    label: "今天测量的舒张压是多少？",
    hint: "测血压时显示的两个数里，偏小的那个",
    kind: "number",
    unit: "mmHg",
  },
  {
    key: "sleep_hours",
    label: "昨晚睡了几个小时？",
    hint: "大概说一个数就行，比如「六个多小时」",
    kind: "number",
    decimal: true,
    unit: "小时",
  },
  {
    key: "mood_score",
    label: "今天心情怎么样？",
    hint: "选一个最贴近的词，1 分很差、5 分很好",
    kind: "choice",
    options: [
      { value: "1", label: "很差" },
      { value: "2", label: "不太好" },
      { value: "3", label: "一般" },
      { value: "4", label: "比较好" },
      { value: "5", label: "很好" },
    ],
  },
  {
    key: "complaint",
    label: "今天身体有什么不舒服？",
    hint: "哪里疼、哪里不得劲，都可以说",
    kind: "choice",
    options: [
      { value: "none", label: "没有" },
      { value: "mild", label: "有点不舒服" },
      { value: "obvious", label: "明显不舒服" },
    ],
  },
];

// ---------------------------------------------------------------------------
// 身份确认候选集（PRD §3.2、FR-VPR-02：候选集只含设备绑定的房间长者）
// ---------------------------------------------------------------------------

export interface IdentityCandidate {
  /** 长者 ID（接口接入后由服务端下发，客户端不猜） */
  id: string;
  /** 展示名 */
  name: string;
  /** 房间 / 床位（展示用） */
  bed: string;
}

/** 双人间候选集 mock 数据（布局验收用，接口待接；仅虚构教学数据） */
export const MOCK_CANDIDATES: IdentityCandidate[] = [
  { id: "elder-demo-a", name: "张桂芬", bed: "302 室 · 靠窗床" },
  { id: "elder-demo-b", name: "李秀兰", bed: "302 室 · 靠门床" },
];
