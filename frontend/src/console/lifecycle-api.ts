/**
 * 生命周期管理 API（Day 9 · C6 · S5，消费 A4：教学重置 / 退房 / 暂离）。
 *
 * **A4 端点尚未定稿交付**（dev-a-安全闸门与生命周期 S4）：本文件把全部路径集中为
 * `LIFECYCLE_ENDPOINTS` 常量，A4 交接通知到达后**只改这一处**；端点 404 时页面显示
 * 「A4 未交付」（骨架先行口径，对齐 memory-api 的 X5 处理）。
 *
 * 二次确认口径（FR-ADM-02 / AC-19）：前端二次确认弹窗前置，确认后才发请求，取消零请求；
 * A4 若定稿为 confirm token 双段式（先请求令牌再携带令牌执行），只改本文件函数实现，
 * 页面层不感知。
 *
 * 语义依据：E8 长者状态（checkout → archived、away → active/away 暂离）、
 * FR-ADM-02 教学数据重置、FR-ADM-09 退房联动（pad 401 / family 403 由联调确认）。
 */

import { apiPost } from "./api";

// ---------------------------------------------------------------------------
// 路径常量（A4 交接后只改这里）
// ---------------------------------------------------------------------------

const LIFECYCLE_BASE = "/api/lifecycle";

export const LIFECYCLE_ENDPOINTS = {
  /** 教学数据重置（危险操作，清演示数据） */
  teachingReset: `${LIFECYCLE_BASE}/teaching-reset`,
  /** 长者退房（→ archived，联动 pad/family 失效） */
  checkout: `${LIFECYCLE_BASE}/checkout`,
  /** 长者暂离开关（active ↔ away） */
  away: `${LIFECYCLE_BASE}/away`,
} as const;

// ---------------------------------------------------------------------------
// 错误归一
// ---------------------------------------------------------------------------

export type LifecycleErrorKind =
  | "not_delivered" // 404：A4 端点未交付
  | "forbidden" // 403：越权（admin-only）
  | "not_found" // 404：目标长者不存在（A4 交付后与 not_delivered 区分）
  | "conflict" // 409：状态不允许（如已退房再退房）
  | "server";

export class LifecycleApiError extends Error {
  constructor(
    public readonly kind: LifecycleErrorKind,
    message: string,
  ) {
    super(message);
    this.name = "LifecycleApiError";
  }
}

async function readErrorDetail(res: Response): Promise<string> {
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  return body?.detail ?? "操作失败，请稍后重试";
}

/** 统一状态码分支（apiPost 已把 401 归一为 ApiError，这里处理其余） */
async function assertOk(res: Response): Promise<void> {
  if (res.ok) return; // 200/204 均视为成功
  const detail = await readErrorDetail(res);
  if (res.status === 404) {
    // A4 未交付阶段端点整体 404；交付后此码仅代表目标不存在
    throw new LifecycleApiError("not_delivered", `A4 未交付（端点不存在）：${detail}`);
  }
  if (res.status === 403) {
    throw new LifecycleApiError("forbidden", "无权限执行该操作（仅管理员）");
  }
  if (res.status === 409) {
    throw new LifecycleApiError("conflict", detail);
  }
  throw new LifecycleApiError("server", detail);
}

// ---------------------------------------------------------------------------
// 三个生命周期操作（均需页面二次确认弹窗后才调用）
// ---------------------------------------------------------------------------

/** 教学数据重置（FR-ADM-02）：确认弹窗通过后调用 */
export async function teachingReset(): Promise<void> {
  const res = await apiPost(LIFECYCLE_ENDPOINTS.teachingReset, { confirm: true });
  await assertOk(res);
}

/** 长者退房（FR-ADM-09）：确认弹窗通过后调用；成功后该长者 archived */
export async function checkoutElder(elderId: string): Promise<void> {
  const res = await apiPost(LIFECYCLE_ENDPOINTS.checkout, {
    elder_id: elderId,
    confirm: true,
  });
  await assertOk(res);
}

/** 暂离开关（E8 away 语义）：确认弹窗通过后调用 */
export async function setElderAway(elderId: string, away: boolean): Promise<void> {
  const res = await apiPost(LIFECYCLE_ENDPOINTS.away, {
    elder_id: elderId,
    away,
  });
  await assertOk(res);
}
