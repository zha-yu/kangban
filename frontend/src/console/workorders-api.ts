/**
 * 工单看板 Mock API（task-06 F2，console 第 3 批首项）。
 *
 * 字段口径唯一依据：技术设计文档 §3.4 `work_orders` / `work_order_transitions` 两表。
 * 业务规则依据：PRD §4.7（状态机 / 管理员重开）+ FR-ALT-01/02（2 分钟升级 / 红档置顶 / 全程留痕）。
 *
 * 后端工单接口尚未交付（f2 计划 D1）：本模块为 Mock 层——TS 类型对齐表字段，
 * 内存虚构教学数据 + 模拟延迟。真实接口交付后**仅替换本文件内部实现**，
 * 导出签名保持不变，页面层不感知（唯一切换点）。
 */

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
// Mock 数据（虚构教学数据，不含真实长者信息）
// ---------------------------------------------------------------------------

const NOW = Date.now();
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString();
const MIN = 60_000;

function transition(
  orderNo: number,
  from: WorkOrderStatus,
  to: WorkOrderStatus,
  msAgo: number,
  actor: [string, string] | null,
  note?: string,
): WorkOrderTransition {
  const n = String(orderNo).padStart(2, "0");
  return {
    transition_id: `t-mock-${n}-${to}`,
    order_id: `wo-mock-${n}`,
    from_status: from,
    to_status: to,
    actor_id: actor?.[0] ?? null,
    actor_name: actor?.[1] ?? null,
    at: iso(msAgo),
    note: note ?? null,
  };
}

const ACK_SYSTEM: [string, string] = ["sys", "系统"];
const LI_CHEN: [string, string] = ["staff-li", "李晨"];

const MOCK_ORDERS: WorkOrder[] = [
  {
    order_id: "wo-mock-01",
    order_no: "WO-20261106-0001",
    elder_id: "elder-301",
    elder_name: "王桂芳",
    room_label: "301 房 · 王桂芳",
    type: "sos",
    severity: "red",
    status: "new",
    assignee_id: null,
    assignee_name: null,
    false_alarm: false,
    false_alarm_reason: null,
    actual_handled_at: null,
    note: null,
    created_at: iso(40 * 1000), // 40 秒前生成 → 倒计时展示中
    closed_at: null,
    transitions: [transition(1, "new", "new", 40 * 1000, ACK_SYSTEM, "SOS 按钮触发，自动建单")],
  },
  {
    order_id: "wo-mock-02",
    order_no: "WO-20261106-0002",
    elder_id: "elder-302",
    elder_name: "张淑英",
    room_label: "302 房 · 张淑英",
    type: "sensitive_alert",
    severity: "red",
    status: "new",
    assignee_id: null,
    assignee_name: null,
    false_alarm: false,
    false_alarm_reason: null,
    actual_handled_at: null,
    note: null,
    created_at: iso(8 * MIN), // 红档未确认超 2 分钟 → escalated 徽标展示
    closed_at: null,
    transitions: [
      transition(2, "new", "new", 8 * MIN, ACK_SYSTEM, "连续 3 句异常情绪话术，规则层命中"),
    ],
  },
  {
    order_id: "wo-mock-03",
    order_no: "WO-20261106-0003",
    elder_id: "elder-305",
    elder_name: "陈守义",
    room_label: "305 房 · 陈守义",
    type: "health_alert",
    severity: "red",
    status: "in_progress",
    assignee_id: "staff-li",
    assignee_name: "李晨",
    false_alarm: false,
    false_alarm_reason: null,
    actual_handled_at: null,
    note: null,
    created_at: iso(25 * MIN),
    closed_at: null,
    transitions: [
      transition(3, "new", "new", 25 * MIN, ACK_SYSTEM, "收缩压 ≥180，健康采集红标"),
      transition(3, "new", "acknowledged", 22 * MIN, LI_CHEN),
      transition(3, "acknowledged", "in_progress", 18 * MIN, LI_CHEN),
    ],
  },
  {
    order_id: "wo-mock-04",
    order_no: "WO-20261106-0004",
    elder_id: "elder-303",
    elder_name: "周玉兰",
    room_label: "303 房 · 周玉兰",
    type: "missed_medication",
    severity: "yellow",
    status: "new",
    assignee_id: null,
    assignee_name: null,
    false_alarm: false,
    false_alarm_reason: null,
    actual_handled_at: null,
    note: null,
    created_at: iso(15 * MIN),
    closed_at: null,
    transitions: [transition(4, "new", "new", 15 * MIN, ACK_SYSTEM, "晚 18:00 剂次已送达未确认")],
  },
  {
    order_id: "wo-mock-05",
    order_no: "WO-20261106-0005",
    elder_id: "elder-306",
    elder_name: "吴建邦",
    room_label: "306 房 · 吴建邦",
    type: "follow_up",
    severity: "yellow",
    status: "resolved",
    assignee_id: "staff-li",
    assignee_name: "李晨",
    false_alarm: false,
    false_alarm_reason: null,
    actual_handled_at: null,
    note: "已上门沟通，长者同意明日按时服药",
    created_at: iso(3 * 60 * MIN),
    closed_at: null,
    transitions: [
      transition(5, "new", "new", 3 * 60 * MIN, ACK_SYSTEM, "午间剂次拒服，人工跟进"),
      transition(5, "new", "acknowledged", 170 * MIN, LI_CHEN),
      transition(5, "acknowledged", "in_progress", 150 * MIN, LI_CHEN),
      transition(5, "in_progress", "resolved", 60 * MIN, LI_CHEN, "已上门沟通，长者同意明日按时服药"),
    ],
  },
  {
    order_id: "wo-mock-06",
    order_no: "WO-20261106-0006",
    elder_id: "elder-304",
    elder_name: "赵金平",
    room_label: "304 房 · 赵金平",
    type: "device_fault",
    severity: "yellow",
    status: "closed",
    assignee_id: "staff-li",
    assignee_name: "李晨",
    false_alarm: true,
    false_alarm_reason: "平板误触紧急呼叫按钮，现场确认无需处置",
    actual_handled_at: null,
    note: "误报关闭，保留原始输入",
    created_at: iso(5 * 60 * MIN),
    closed_at: iso(270 * MIN),
    transitions: [
      transition(6, "new", "new", 5 * 60 * MIN, ACK_SYSTEM, "设备离线告警"),
      transition(6, "new", "acknowledged", 290 * MIN, LI_CHEN),
      transition(6, "acknowledged", "closed", 270 * MIN, LI_CHEN, "误报：平板误触紧急呼叫按钮，现场确认无需处置"),
    ],
  },
  {
    order_id: "wo-mock-07",
    order_no: "WO-20261106-0007",
    elder_id: "elder-302",
    elder_name: "张淑英",
    room_label: "302 房 · 张淑英",
    type: "collection_missed",
    severity: "yellow",
    status: "closed",
    assignee_id: "staff-li",
    assignee_name: "李晨",
    false_alarm: false,
    false_alarm_reason: null,
    actual_handled_at: null,
    note: "长者入睡拒采，次日补采完成",
    created_at: iso(26 * 60 * MIN),
    closed_at: iso(20 * 60 * MIN),
    transitions: [
      transition(7, "new", "new", 26 * 60 * MIN, ACK_SYSTEM, "昨日 19:00 采集窗口漏采，自动派单"),
      transition(7, "new", "acknowledged", 25 * 60 * MIN, LI_CHEN),
      transition(7, "acknowledged", "in_progress", 24 * 60 * MIN, LI_CHEN),
      transition(7, "in_progress", "resolved", 21 * 60 * MIN, LI_CHEN, "已安排次日补采"),
      transition(7, "resolved", "closed", 20 * 60 * MIN, LI_CHEN, "次日补采完成"),
    ],
  },
];

let store: WorkOrder[] = MOCK_ORDERS.map((o) => ({ ...o, transitions: [...o.transitions] }));

// ---------------------------------------------------------------------------
// Mock 行为（async + 模拟延迟；真实接口交付后仅替换本节实现）
// ---------------------------------------------------------------------------

const MOCK_DELAY_MS = 200;

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), MOCK_DELAY_MS));
}

export class MockApiError extends Error {
  constructor(
    public readonly code: "invalid_transition" | "missing_field",
    message: string,
  ) {
    super(message);
    this.name = "MockApiError";
  }
}

const OPEN_STATUSES: ReadonlySet<WorkOrderStatus> = new Set([
  "new",
  "acknowledged",
  "in_progress",
  "reopened",
]);

/** 看板列表（Mock 一次返回全量，排序/筛选在页面层做——真实接口保持同签名） */
export async function listWorkOrders(): Promise<WorkOrder[]> {
  return delay(store.map((o) => ({ ...o })));
}

/** 状态机流转（PRD §4.7；全程留痕 work_order_transitions） */
export async function transitionWorkOrder(
  orderId: string,
  action: TransitionAction,
  payload: TransitionPayload,
  actor: { id: string; name: string },
): Promise<WorkOrder> {
  const idx = store.findIndex((o) => o.order_id === orderId);
  if (idx === -1) throw new MockApiError("invalid_transition", "工单不存在");

  const order = store[idx];
  const from = order.status;
  let to: WorkOrderStatus;

  switch (action) {
    case "acknowledge":
      if (from !== "new" && from !== "reopened") {
        throw new MockApiError("invalid_transition", "仅待确认或已重开的工单可确认");
      }
      to = "acknowledged";
      break;
    case "start":
      if (from !== "acknowledged") {
        throw new MockApiError("invalid_transition", "仅已确认的工单可开始处理");
      }
      to = "in_progress";
      break;
    case "resolve":
      if (from !== "in_progress") {
        throw new MockApiError("invalid_transition", "仅处理中的工单可解决");
      }
      if (order.severity === "red" && !payload.actual_handled_at) {
        throw new MockApiError("missing_field", "红档工单必须填写实际处置时间");
      }
      if (!payload.note) {
        throw new MockApiError("missing_field", "解决时必须填写处置备注");
      }
      to = "resolved";
      break;
    case "close":
      if (from !== "resolved") {
        throw new MockApiError("invalid_transition", "仅已解决的工单可关闭");
      }
      if (!payload.note) {
        throw new MockApiError("missing_field", "关闭必填处置结果");
      }
      to = "closed";
      break;
    case "false_alarm":
      if (!OPEN_STATUSES.has(from)) {
        throw new MockApiError("invalid_transition", "已关闭的工单不能标误报");
      }
      if (!payload.note) {
        throw new MockApiError("missing_field", "误报必填原因");
      }
      to = "closed";
      break;
    case "reopen":
      if (from !== "closed") {
        throw new MockApiError("invalid_transition", "仅已关闭的工单可重开");
      }
      if (!payload.note) {
        throw new MockApiError("missing_field", "重开必填原因");
      }
      to = "reopened";
      break;
  }

  const now = new Date().toISOString();
  const updated: WorkOrder = { ...order };

  if (action === "acknowledge") {
    updated.assignee_id = actor.id;
    updated.assignee_name = actor.name;
  }
  if (action === "resolve") {
    updated.note = payload.note ?? null;
    updated.actual_handled_at = payload.actual_handled_at
      ? new Date(payload.actual_handled_at).toISOString()
      : null;
  }
  if (action === "close" || action === "false_alarm") {
    if (action === "false_alarm") {
      updated.false_alarm = true;
      updated.false_alarm_reason = payload.note ?? null;
    } else {
      updated.note = payload.note ?? null;
    }
    updated.closed_at = now;
  }
  updated.status = to;

  const record: WorkOrderTransition = {
    transition_id: `t-${order.order_id}-${Date.now()}`,
    order_id: order.order_id,
    from_status: from,
    to_status: to,
    actor_id: actor.id,
    actor_name: actor.name,
    at: now,
    note: payload.note ?? null,
  };

  store = store.map((o, i) =>
    i === idx ? { ...updated, transitions: [...o.transitions, record] } : o,
  );
  return delay({ ...store[idx] });
}

/** 管理员重开入口（f2 D3：页面层护工不显示，API 保留供管理员页复用） */
export function isReopenAllowed(role: string | null): boolean {
  return role === "admin";
}
