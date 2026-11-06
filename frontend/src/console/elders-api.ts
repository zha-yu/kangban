/**
 * 负责长者 API 封装（task-07 F3，console 首个真实对接业务页）。
 *
 * 契约唯一依据：docs/接口契约_身份与权限.md §E7 / §E6 / §R4 / S-05 / S-07。
 * - E7 `GET /api/elders`：分页列表，服务端按角色范围过滤（护工=负责长者，AC-18）；
 *   列表项不返敏感字段（S-07）。
 * - E6 `GET /api/elders/{elder_id}`：R4 全字段 + room_no/bed_no（S-05 JOIN 回填）；
 *   403 非负责护工/医生、404 仅对范围内主体。
 */

import { apiGet } from "./api";

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
