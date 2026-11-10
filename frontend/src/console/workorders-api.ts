/**
 * 工单看板 API（task-06 F2 交付 Mock 层，f2-D1 真实接口已切换）。
 *
 * 字段口径唯一依据：技术设计文档 §3.4 `work_orders` / `work_order_transitions` 两表。
 * 业务规则依据：PRD §4.7（状态机 / 管理员重开）+ FR-ALT-01/02（2 分钟升级 / 红档置顶 / 全程留痕）。
 *
 * 2026-11-10：后端 `GET/POST /api/work-orders*` 已交付（app/api/workorders.py），
 * 本文件已从 Mock 层切换为真实接口；导出签名不变，页面层无感知。
 * 状态机合法性校验由后端执行（422 → MockApiError），前端不再本地判规则。
 */

import { apiGet, apiPost } from "./api";

// ---------------------------------------------------------------------------
// 类型（对齐 work_orders / work_order_transitions 表字段）
// ---------------------------------------------------------------------------

export type WorkOrderType =
  | "missed_medication" // 漏服药
  | "sensitive_alert" // 敏感告警
  | "health_alert" // 健康告警
  | "device_fault" // 设备故障
  | "sos" // 一键求助
  | "collection_missed" // 健康漏采（FR-HLT-08）
  | "drug_question" // 药品疑问
  | "follow_up"; // 拒服跟进（AC-07）

export type WorkOrderStatus =
  | "new"
  | "acknowledged"
  | "in_progress"
  | "resolved"
  | "closed"
  | "reopened"; // 关闭后重开（PRD §4.7：管理员可重开）

export type Severity = "red" | "yellow";

/** 看板操作（状态机动作，PRD §4.7） */
export type TransitionAction =
  | "acknowledge" // new→acknowledged 确认
  | "start" // acknowledged→in_progress 开始处理
  | "resolve" // in_progress→resolved 解决（红档强制 actualHandledAt + note）
  | "close" // resolved→closed 关闭（处置结果 note 必填）
  | "false_alarm" // 未关闭→closed 误报关闭（原因必填，保留原始输入）
  | "reopen"; // closed→reopened（仅管理员，原因必填）

export interface WorkOrderTransition {
  transition_id: string;
  order_id: string;
  from_status: WorkOrderStatus;
  to_status: WorkOrderStatus;
  actor_id: string | null; // 系统自动流转可空
  actor_name: string | null;
  at: string; // ISO UTC
  note: string | null; // 如 reopen 原因
}

/** 对齐 work_orders 表（建单快照口径：severity 不随后同步） */
export interface WorkOrder {
  order_id: string;
  order_no: string;
  elder_id: string;
  elder_name: string; // 冗余展示字段（真实接口由服务端 join 返回）
  room_label: string; // 冗余展示字段（如 "301 房 · 王桂芳"）
  type: WorkOrderType;
  severity: Severity;
  status: WorkOrderStatus;
  assignee_id: string | null;
  assignee_name: string | null;
  false_alarm: boolean;
  false_alarm_reason: string | null;
  actual_handled_at: string | null; // 红色事件解决时必填
  note: string | null;
  created_at: string; // ISO UTC（升级计时起点，不因通知失败暂停）
  closed_at: string | null;
  transitions: WorkOrderTransition[];
}

export interface TransitionPayload {
  /** resolve：红档必填（实际处置时间）；close：处置结果；false_alarm / reopen：原因 */
  note?: string;
  /** resolve 红档必填：实际处置时间（datetime-local 值） */
  actual_handled_at?: string;
}

// ---------------------------------------------------------------------------
// 类型展示文案（页面层共用）
// ---------------------------------------------------------------------------

export const TYPE_LABELS: Record<WorkOrderType, string> = {
  missed_medication: "漏服药",
  sensitive_alert: "敏感告警",
  health_alert: "健康告警",
  device_fault: "设备故障",
  sos: "一键求助",
  collection_missed: "健康漏采",
  drug_question: "药品疑问",
  follow_up: "拒服跟进",
};

export const STATUS_LABELS: Record<WorkOrderStatus, string> = {
  new: "待确认",
  acknowledged: "已确认",
  in_progress: "处理中",
  resolved: "已解决",
  closed: "已关闭",
  reopened: "已重开",
};

export const SEVERITY_LABELS: Record<Severity, string> = {
  red: "红档",
  yellow: "黄档",
};

/** 红色事件升级时限（FR-ALT-01：2 分钟无人确认 → escalated） */
export const ESCALATION_MS = 2 * 60 * 1000;


// ---------------------------------------------------------------------------
// 真实接口（后端 app/api/workorders.py；操作人从 JWT 注入，前端值仅兼容签名）
// ---------------------------------------------------------------------------

export class MockApiError extends Error {
  constructor(
    public readonly code: "invalid_transition" | "missing_field",
    message: string,
  ) {
    super(message);
    this.name = "MockApiError";
  }
}

/** 看板列表（后端一次返回全量，排序/筛选在页面层做——与 Mock 口径一致） */
export async function listWorkOrders(): Promise<WorkOrder[]> {
  return apiGet<WorkOrder[]>("/api/work-orders");
}

async function readErrorDetail(res: Response): Promise<string> {
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  return body?.detail ?? "操作失败";
}

/** 状态机流转（PRD §4.7；全程留痕 work_order_transitions） */
export async function transitionWorkOrder(
  orderId: string,
  action: TransitionAction,
  payload: TransitionPayload,
  actor: { id: string; name: string },
): Promise<WorkOrder> {
  const res = await apiPost(`/api/work-orders/${orderId}/transitions`, {
    action,
    note: payload.note ?? null,
    actual_handled_at: payload.actual_handled_at
      ? new Date(payload.actual_handled_at).toISOString() // datetime-local → ISO UTC
      : null,
    actor_id: actor.id, // 仅兼容签名，后端从 JWT 注入并丢弃此值
  });
  if (res.ok) return (await res.json()) as WorkOrder;
  const message = await readErrorDetail(res);
  if (res.status === 404) throw new MockApiError("invalid_transition", "工单不存在");
  if (res.status === 403) throw new MockApiError("invalid_transition", message);
  throw new MockApiError("invalid_transition", message); // 422（状态机/必填校验）与其余
}

/** 管理员重开入口（f2 D3：页面层护工不显示，API 保留供管理员页复用） */
export function isReopenAllowed(role: string | null): boolean {
  return role === "admin";
}
