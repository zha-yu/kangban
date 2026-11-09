/**
 * 管理员 · 房间/床位管理 API 封装（实施计划 v1.5 §3 / §6）。
 *
 * 端点前缀 `/api/admin`：房间分页/新建/改房号/删除，床位分页/新建/改床号或状态/删除，
 * 以及批量改状态、批量删空床。E18 `GET /api/rooms` 为冻结基线，本模块不碰。
 *
 * 错误处置沿用 K2-B：状态码在本模块解析为页面级文案，`console/api.ts` 只补 PATCH/DELETE；
 * 文案用用户语言，不回显 trace_id、detail 原文或内部实现细节。
 */

import { AdminApiError } from "./admin-api";
import { ApiError, NETWORK_ERROR_TEXT, apiDelete, apiGetWithStatus, apiPatch, apiPost } from "./api";

export type RoomStatus = "available" | "partial" | "full" | "maintenance";
/** `occupied` 由 beds.elder_id 派生，只能由入住/退床改变，不允许手工写入 */
export type BedStatus = "available" | "occupied" | "maintenance" | "disabled";
/** 可手工设置的床位状态（不含 occupied） */
export type BedManualStatus = Exclude<BedStatus, "occupied">;

export interface AdminRoom {
  room_id: string;
  room_no: string;
  status: RoomStatus;
  /** 后端若只回床位摘要，页面用 beds 派生计数（三类字段至少有一组） */
  bed_count?: number;
  occupied_count?: number;
  beds?: Array<{ bed_id: string; bed_no: string; elder_id: string | null }>;
}

export interface AdminBed {
  bed_id: string;
  room_id: string;
  room_no: string;
  bed_no: string;
  status: BedStatus;
  elder_id: string | null;
  /** 可选：后端 JOIN 回填长者姓名；未返回时页面降级展示 ID 前缀 */
  elder_name?: string | null;
}

export interface AdminRoomListResult {
  items: AdminRoom[];
  total: number;
  page: number;
  page_size: number;
}

export interface AdminBedListResult {
  items: AdminBed[];
  total: number;
  page: number;
  page_size: number;
}

export interface AdminRoomQuery {
  page: number;
  pageSize: number;
  roomNo?: string;
  status?: RoomStatus | "";
}

export interface AdminBedQuery {
  page: number;
  pageSize: number;
  roomId?: string;
  bedNo?: string;
  status?: BedStatus | "";
}

/** 状态码 → 页面级错误（409 文案随业务场景不同，由调用方给出） */
function errorFromStatus(status: number, conflictText: string): AdminApiError {
  switch (status) {
    case 403:
      return new AdminApiError("forbidden", "当前账号无权执行房间/床位管理操作");
    case 409:
      return new AdminApiError("conflict", conflictText);
    case 404:
      return new AdminApiError("not_found", "目标不存在，请刷新列表后重试");
    case 422:
      return new AdminApiError("validation", "提交内容不符合要求，请按提示检查后重试");
    case 400:
      return new AdminApiError("bad_request", "请求格式有误，请刷新页面后重试");
    default:
      return new AdminApiError("network", "服务暂时不可用，请稍后重试");
  }
}

/** 204 / 空响应体按无内容处理，避免对 204 调 json() 抛错 */
async function readBody<T>(res: Response): Promise<T | null> {
  if (res.status === 204) return null;
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

async function adminGet<T>(path: string, conflictText: string): Promise<T> {
  let result: { status: number; body: T | null };
  try {
    result = await apiGetWithStatus<T>(path);
  } catch (err) {
    if (err instanceof ApiError) {
      throw new AdminApiError(err.kind === "invalid" ? "invalid" : "network", err.message);
    }
    throw new AdminApiError("network", NETWORK_ERROR_TEXT);
  }
  if (result.body !== null) return result.body;
  throw errorFromStatus(result.status, conflictText);
}

async function adminSend<T>(
  send: () => Promise<Response>,
  conflictText: string,
): Promise<T | null> {
  let res: Response;
  try {
    res = await send();
  } catch (err) {
    if (err instanceof ApiError) {
      throw new AdminApiError(err.kind === "invalid" ? "invalid" : "network", err.message);
    }
    throw new AdminApiError("network", NETWORK_ERROR_TEXT);
  }
  if (!res.ok) throw errorFromStatus(res.status, conflictText);
  return readBody<T>(res);
}

// ---------------------------------------------------------------------------
// 房间
// ---------------------------------------------------------------------------

/** GET /api/admin/rooms —— 房间分页（含床位数与占用数） */
export function fetchAdminRooms({ page, pageSize, roomNo, status }: AdminRoomQuery): Promise<AdminRoomListResult> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (roomNo?.trim()) params.set("room_no", roomNo.trim());
  if (status) params.set("status", status);
  return adminGet<AdminRoomListResult>(`/api/admin/rooms?${params.toString()}`, "房间数据冲突，请刷新后重试");
}

/** POST /api/admin/rooms —— 新建房间（仅房号，房间状态由床位聚合） */
export async function createAdminRoom(roomNo: string): Promise<void> {
  await adminSend<AdminRoom>(() => apiPost("/api/admin/rooms", { room_no: roomNo }), "该房号已存在，请换一个房号");
}

/** PATCH /api/admin/rooms/{room_id} —— 改房号 */
export async function updateAdminRoom(roomId: string, roomNo: string): Promise<void> {
  await adminSend<AdminRoom>(
    () => apiPatch(`/api/admin/rooms/${encodeURIComponent(roomId)}`, { room_no: roomNo }),
    "该房号已存在，请换一个房号",
  );
}

/** DELETE /api/admin/rooms/{room_id} —— 删除房间；有床位或有设备时 409 */
export async function deleteAdminRoom(roomId: string): Promise<void> {
  await adminSend<null>(
    () => apiDelete(`/api/admin/rooms/${encodeURIComponent(roomId)}`),
    "该房间下还有床位或设备，请先处理后再删除",
  );
}

// ---------------------------------------------------------------------------
// 床位
// ---------------------------------------------------------------------------

/** GET /api/admin/beds —— 床位分页/筛选（含所属房号） */
export function fetchAdminBeds({ page, pageSize, roomId, bedNo, status }: AdminBedQuery): Promise<AdminBedListResult> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (roomId) params.set("room_id", roomId);
  if (bedNo?.trim()) params.set("bed_no", bedNo.trim());
  if (status) params.set("status", status);
  return adminGet<AdminBedListResult>(`/api/admin/beds?${params.toString()}`, "床位数据冲突，请刷新后重试");
}

/** POST /api/admin/beds —— 新建床位（初始为空闲） */
export async function createAdminBed(roomId: string, bedNo: string): Promise<void> {
  await adminSend<AdminBed>(
    () => apiPost("/api/admin/beds", { room_id: roomId, bed_no: bedNo }),
    "该房间下已存在相同床号，请换一个床号",
  );
}

/** PATCH /api/admin/beds/{bed_id} —— 改床号/状态；occupied 由入住状态派生，不提交 */
export async function updateAdminBed(
  bedId: string,
  input: { bedNo?: string; status?: BedManualStatus },
): Promise<void> {
  const body: { bed_no?: string; status?: BedManualStatus } = {};
  if (input.bedNo !== undefined) body.bed_no = input.bedNo;
  if (input.status !== undefined) body.status = input.status;
  await adminSend<AdminBed>(
    () => apiPatch(`/api/admin/beds/${encodeURIComponent(bedId)}`, body),
    "该房间下已存在相同床号，或该床位已被占用",
  );
}

/** DELETE /api/admin/beds/{bed_id} —— 删除空床；有入住长者时 409 */
export async function deleteAdminBed(bedId: string): Promise<void> {
  await adminSend<null>(
    () => apiDelete(`/api/admin/beds/${encodeURIComponent(bedId)}`),
    "该床位已入住长者，不能删除",
  );
}

/** POST /api/admin/beds/batch-status —— 批量改状态；任一非法整批拒绝 */
export async function batchUpdateBedStatus(ids: string[], status: BedManualStatus): Promise<void> {
  await adminSend<null>(
    () => apiPost("/api/admin/beds/batch-status", { bed_ids: ids, status }),
    "所选床位包含已入住床位，无法批量改状态",
  );
}

/** POST /api/admin/beds/batch-delete —— 批量删空床；任一占用整批拒绝 */
export async function batchDeleteBeds(ids: string[]): Promise<void> {
  await adminSend<null>(
    () => apiPost("/api/admin/beds/batch-delete", { bed_ids: ids }),
    "所选床位包含已入住床位，无法批量删除",
  );
}
