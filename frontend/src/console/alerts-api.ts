/**
 * 告警通知中心 API（task-08 F4 → Day 8 S6a 切真）。
 *
 * 字段口径唯一依据：docs/接口契约_照护台业务数据.md §3.2 通知中心（N-1 列表 /
 * N-2 已读 / N-3 人工确认）与 §3.4 `notifications` 列清单。
 * 业务规则依据：PRD FR-ALT-03（P0）+ AC-23（通知状态边界：generated→pending→delivered→
 * acknowledged 严格顺序；只有客户端回执算送达、只有人工操作算确认；送达≠确认、
 * 确认≠工单完成）+ 裁决 B5（P0 只做「落库 + 演示卡片」，webpush 归 P1）。
 *
 * - USE_MOCK 开关（Day 8 S6a → S6 翻转）：默认真实接口（N-1/N-2/N-3），
 *   仅显式 `VITE_USE_MOCK_API=1` 回退 Mock 内存层；导出签名不变，页面层不感知。
 * - 状态枚举按 dev-B 核实口径（v1.3 收口）：契约 §3.4 五值（failed 为
 *   pending 30s×3 重试终态，TD §3.5）；正常链路只见四态，页面仍展示 failed。
 * - 真实模式下 actor 入参仅兼容签名——确认人身份由服务端 JWT 注入；
 *   N-3 仅 delivered 可确认（409），N-2 已读 ≠ 确认（AC-23）。
 */

import { apiGet, apiPost, USE_MOCK_API } from "./api";

// ---------------------------------------------------------------------------
// 类型（对齐 notifications 表字段）
// ---------------------------------------------------------------------------

export type NotificationTargetType = "guardian" | "staff";

export type NotificationType =
  | "emergency_health" // 健康红标紧急通知
  | "sensitive" // 敏感告警
  | "missed_medication" // 漏服药
  | "report_ready" // 家属日报就绪
  | "sos"; // 一键求助
  // 技术文档 type 取值为开放枚举（emergency_health/sensitive/missed_medication/report_ready/sos…）

export type NotificationChannel = "inapp" | "webpush";

/** PRD §4.7 通知状态机：generated→pending→delivered→acknowledged；failed 为重试终态 */
export type NotificationStatus =
  | "generated"
  | "pending"
  | "delivered"
  | "acknowledged"
  | "failed";

/** payload 落库已脱敏，前端原样展示 */
export interface NotificationPayload {
  title: string;
  summary: string;
  /** 关联实体（如 order_no），仅展示用 */
  order_no?: string;
  room_label?: string;
}

/** 对齐 notifications 表（staff 侧展示字段） */
export interface NotificationItem {
  notification_id: string;
  target_type: NotificationTargetType;
  target_id: string;
  type: NotificationType;
  payload: NotificationPayload;
  channel: NotificationChannel;
  status: NotificationStatus;
  is_mock: boolean;
  retry_count: number;
  dedupe_key: string | null;
  created_at: string; // 生成时间（重试计时起点）
  sent_at: string | null; // 发送时间（生成后可能延迟）
  acknowledged_by: string | null;
  acknowledged_by_name: string | null;
  acknowledged_at: string | null;
  read_at: string | null;
}

// ---------------------------------------------------------------------------
// 展示文案
// ---------------------------------------------------------------------------

export const NOTIF_TYPE_LABELS: Record<NotificationType, string> = {
  emergency_health: "健康紧急",
  sensitive: "敏感告警",
  missed_medication: "漏服药",
  report_ready: "日报就绪",
  sos: "一键求助",
};

export const NOTIF_STATUS_LABELS: Record<NotificationStatus, string> = {
  generated: "已生成",
  pending: "待送达",
  delivered: "已送达",
  acknowledged: "已确认",
  failed: "发送失败",
};

/** 通知状态机推进顺序（AC-23）：确认按钮仅 delivered 可用，禁止跳步 */
const STATUS_ORDER: NotificationStatus[] = [
  "generated",
  "pending",
  "delivered",
  "acknowledged",
];

// ---------------------------------------------------------------------------
// Mock 数据（虚构教学数据，不含真实长者信息）
// ---------------------------------------------------------------------------

const NOW = Date.now();
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString();
const MIN = 60_000;

function notif(
  n: number,
  type: NotificationType,
  payload: NotificationPayload,
  status: NotificationStatus,
  createdMsAgo: number,
  opts: {
    sentMsAgo?: number | null;
    acknowledgedBy?: [string, string];
    acknowledgedMsAgo?: number;
    readMsAgo?: number | null;
    retryCount?: number;
    isMock?: boolean;
    dedupeKey?: string | null;
    channel?: NotificationChannel;
  } = {},
): NotificationItem {
  return {
    notification_id: `ntf-mock-${String(n).padStart(2, "0")}`,
    target_type: "staff",
    target_id: "staff-li",
    type,
    payload,
    channel: opts.channel ?? "inapp",
    status,
    is_mock: opts.isMock ?? true,
    retry_count: opts.retryCount ?? 0,
    dedupe_key: opts.dedupeKey ?? null,
    created_at: iso(createdMsAgo),
    sent_at: opts.sentMsAgo === undefined ? iso(createdMsAgo - 5_000) : null,
    acknowledged_by: opts.acknowledgedBy?.[0] ?? null,
    acknowledged_by_name: opts.acknowledgedBy?.[1] ?? null,
    acknowledged_at:
      opts.acknowledgedBy && opts.acknowledgedMsAgo !== undefined
        ? iso(opts.acknowledgedMsAgo)
        : null,
    read_at: opts.readMsAgo ? iso(opts.readMsAgo) : null,
  };
}

const MOCK_NOTIFICATIONS: NotificationItem[] = [
  notif(
    1,
    "sos",
    { title: "301 房一键求助", summary: "长者按下床头 SOS 按钮，已生成紧急工单 WO-20261106-0001", order_no: "WO-20261106-0001", room_label: "301 房 · 王桂芳" },
    "delivered",
    30 * 1000,
    { readMsAgo: null },
  ),
  notif(
    2,
    "emergency_health",
    { title: "健康红标", summary: "305 房 · 陈守义 收缩压 185mmHg（≥180），已生成紧急工单", order_no: "WO-20261106-0003", room_label: "305 房 · 陈守义" },
    "pending",
    4 * MIN,
    { sentMsAgo: null },
  ),
  notif(
    3,
    "sensitive",
    { title: "敏感告警", summary: "302 房 · 张淑英 连续异常情绪话术，规则层命中并建单", order_no: "WO-20261106-0002", room_label: "302 房 · 张淑英" },
    "acknowledged",
    9 * MIN,
    {
      acknowledgedBy: ["staff-li", "李晨"],
      acknowledgedMsAgo: 5 * MIN,
      readMsAgo: 6 * MIN,
    },
  ),
  notif(
    4,
    "missed_medication",
    { title: "漏服药提醒", summary: "303 房 · 周玉兰 晚 18:00 剂次已送达未确认", order_no: "WO-20261106-0004", room_label: "303 房 · 周玉兰" },
    "generated",
    2 * MIN,
    { sentMsAgo: null },
  ),
  notif(
    5,
    "report_ready",
    { title: "家属日报已生成", summary: "303 房 · 周玉兰 11-05 日报 v1 已就绪（dedupe：同版本只发一次）" },
    "acknowledged",
    14 * 60 * MIN,
    {
      acknowledgedBy: ["staff-li", "李晨"],
      acknowledgedMsAgo: 13 * 60 * MIN,
      readMsAgo: 13 * 60 * MIN,
      dedupeKey: "daily-report:303:20261105:v1",
    },
  ),
  notif(
    6,
    "emergency_health",
    { title: "健康红标（重试失败）", summary: "306 房 · 吴建邦 心率异常事件通知，30s×3 次重试后失败" },
    "failed",
    40 * MIN,
    { retryCount: 3 },
  ),
  notif(
    7,
    "sensitive",
    { title: "敏感告警（演示卡片）", summary: "305 房 · 陈守义 演示用模拟通知（不计入送达率分母）" },
    "delivered",
    60 * MIN,
    { isMock: true, channel: "webpush", readMsAgo: 55 * MIN },
  ),
  notif(
    8,
    "report_ready",
    { title: "家属日报已生成（guardian 目标，不可操作）", summary: "302 房 · 张淑英 11-05 日报已推送至监护人（展示用）" },
    "acknowledged",
    20 * 60 * MIN,
    {
      acknowledgedBy: ["staff-li", "李晨"],
      acknowledgedMsAgo: 19 * 60 * MIN,
      readMsAgo: 19 * 60 * MIN,
    },
  ),
];

let store: NotificationItem[] = MOCK_NOTIFICATIONS.map((n) => ({ ...n, payload: { ...n.payload } }));

// ---------------------------------------------------------------------------
// Mock 行为（真实接口交付后仅替换本节实现）
// ---------------------------------------------------------------------------

const MOCK_DELAY_MS = 200;

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), MOCK_DELAY_MS));
}

export class MockApiError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MockApiError";
  }
}

/** 契约错误文案提取（workorders-api 同构） */
async function readErrorDetail(res: Response): Promise<string> {
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  return body?.detail ?? "操作失败，请稍后重试";
}

/** staff 通知列表（护工侧只看 target_type=staff；真实接口由服务端按身份过滤） */
export async function listNotifications(): Promise<NotificationItem[]> {
  if (USE_MOCK_API) {
    return delay(
      store
        .filter((n) => n.target_type === "staff")
        .map((n) => ({ ...n, payload: { ...n.payload } })),
    );
  }
  const res = await apiGet<{ items: NotificationItem[] }>("/api/notifications");
  return res.items;
}

/** 打开详情 = 已读（AC-23：已读 ≠ 确认；N-2） */
export async function markNotificationRead(notificationId: string): Promise<NotificationItem> {
  if (USE_MOCK_API) {
    const idx = store.findIndex((n) => n.notification_id === notificationId);
    if (idx === -1) throw new MockApiError("通知不存在");
    if (!store[idx].read_at) {
      store = store.map((n, i) =>
        i === idx ? { ...n, read_at: new Date().toISOString() } : n,
      );
    }
    return delay({ ...store[idx], payload: { ...store[idx].payload } });
  }
  const res = await apiPost(`/api/notifications/${notificationId}/read`, {});
  if (res.ok) return (await res.json()) as NotificationItem;
  if (res.status === 404) throw new MockApiError("通知不存在");
  throw new MockApiError(await readErrorDetail(res));
}

/** 人工确认（AC-23：只有人工操作算确认；仅 delivered 可确认，禁止跳步；N-3） */
export async function acknowledgeNotification(
  notificationId: string,
  actor: { id: string; name: string },
): Promise<NotificationItem> {
  if (USE_MOCK_API) {
    const idx = store.findIndex((n) => n.notification_id === notificationId);
    if (idx === -1) throw new MockApiError("通知不存在");
    const item = store[idx];
    if (item.status !== "delivered") {
      throw new MockApiError("仅已送达的通知可人工确认");
    }
    const now = new Date().toISOString();
    store = store.map((n, i) =>
      i === idx
        ? { ...n, status: "acknowledged", acknowledged_by: actor.id, acknowledged_by_name: actor.name, acknowledged_at: now }
        : n,
    );
    return delay({ ...store[idx], payload: { ...store[idx].payload } });
  }
  void actor; // 确认人身份由服务端 JWT 注入，入参仅兼容签名
  const res = await apiPost(`/api/notifications/${notificationId}/acknowledge`, {});
  if (res.ok) return (await res.json()) as NotificationItem;
  if (res.status === 404) throw new MockApiError("通知不存在");
  if (res.status === 409) throw new MockApiError("仅已送达的通知可人工确认");
  throw new MockApiError(await readErrorDetail(res));
}

/** 页面导出：状态推进顺序（时间线渲染用） */
export const NOTIF_STATUS_FLOW = STATUS_ORDER;
