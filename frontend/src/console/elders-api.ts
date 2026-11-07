/**
 * 负责长者 API 封装（task-07 F3，console 首个真实对接业务页）。
 *
 * 契约唯一依据：docs/接口契约_身份与权限.md §E7 / §E6 / §R4 / S-05 / S-07。
 * - E7 `GET /api/elders`：分页列表，服务端按角色范围过滤（护工=负责长者，AC-18）；
 *   列表项不返敏感字段（S-07）。
 * - E6 `GET /api/elders/{elder_id}`：R4 全字段 + room_no/bed_no（S-05 JOIN 回填）；
 *   403 非负责护工/医生、404 仅对范围内主体。
 */

import { apiGet, apiGetWithStatus, apiPost } from "./api";

export type ElderStatus = "active" | "away" | "transferred" | "archived";

export const ELDER_STATUS_LABELS: Record<ElderStatus, string> = {
  active: "在住",
  away: "外出",
  transferred: "转出",
  archived: "已归档",
};

/** E7 列表项（S-07：不返 birth_date/gender/persona_id） */
export interface ElderListItem {
  elder_id: string;
  name: string;
  room_no: string | null;
  bed_no: string | null;
  status: ElderStatus;
}

export interface ElderListResult {
  items: ElderListItem[];
  total: number;
  page: number;
  page_size: number;
}

/** E6 详情：R4 全字段 + 房间床号（S-05） */
export interface ElderDetail extends ElderListItem {
  birth_date: string | null;
  gender: string | null;
  persona_id: string | null;
  session_seq: number;
  created_at: string;
}

export interface ElderListQuery {
  page: number;
  pageSize: number;
  status?: ElderStatus;
}

export async function listElders({ page, pageSize, status }: ElderListQuery): Promise<ElderListResult> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (status) params.set("status", status);
  return apiGet<ElderListResult>(`/api/elders?${params.toString()}`);
}

export async function getElder(elderId: string): Promise<ElderDetail> {
  return apiGet<ElderDetail>(`/api/elders/${encodeURIComponent(elderId)}`);
}

// ---------------------------------------------------------------------------
// 注册向导接口（task-08 F2：E5 建档 → E19 入床 → E16 建设备 / E9 绑定，契约 v1.8）
// ---------------------------------------------------------------------------

export interface RoomBed {
  bed_id: string;
  bed_no: string;
  elder_id: string | null;
}

export interface RoomWithBeds {
  room_id: string;
  room_no: string;
  status: string;
  beds: RoomBed[];
}

export interface DeviceItem {
  device_id: string;
  room_id: string;
  room_no: string;
  elder_id: string | null;
  status: string;
}

/** E16 建档响应：device_secret 为一次性明文，仅本次可见 */
export interface DeviceCreated {
  device_id: string;
  room_id: string;
  status: string;
  device_secret: string;
}

/** E5 长者建档；返回原始 Response，403/409 等由调用方按契约分支 */
export function createElder(body: {
  name: string;
  birth_date?: string;
  gender?: string;
  persona?: string;
}): Promise<Response> {
  return apiPost("/api/elders", body);
}

/** E18 房间床位列表（员工全量可读） */
export function fetchRooms(): Promise<RoomWithBeds[]> {
  return apiGetWithStatus<{ items: RoomWithBeds[] }>("/api/rooms").then(
    (r) => r.body?.items ?? [],
  );
}

/** E19 长者入床 */
export function assignBed(elderId: string, bedId: string): Promise<Response> {
  return apiPost(`/api/elders/${encodeURIComponent(elderId)}/bed`, { bed_id: bedId });
}

/** E17 设备列表（含未绑定） */
export function fetchDevices(): Promise<DeviceItem[]> {
  return apiGetWithStatus<{ items: DeviceItem[] }>("/api/devices").then(
    (r) => r.body?.items ?? [],
  );
}

/** E16 设备建档（响应含一次性明文 secret，展示一次后不可再取） */
export function createDevice(roomId: string): Promise<DeviceCreated> {
  return apiPost("/api/devices", { room_id: roomId }).then((res) => {
    if (!res.ok) throw res.status;
    return res.json() as Promise<DeviceCreated>;
  });
}

/** E9 设备绑定（admin/care_worker） */
export function bindDevice(deviceId: string, roomId: string, elderId: string): Promise<Response> {
  return apiPost(`/api/devices/${encodeURIComponent(deviceId)}/bind`, {
    room_id: roomId,
    elder_id: elderId,
  });
}
