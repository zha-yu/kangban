/**
 * 记忆管理 API（Day 9 · C6 · S5，消费 app/api/memory.py · X5）。
 *
 * 契约依据：GET /api/memory/{elder_id}/memories（limit/offset 分页）、
 * PATCH /api/memory/{elder_id}/memories/{chunk_id}（纠正：旧值 superseded、新值生效，
 * FR-MEM-03/06）、DELETE 同路径（删除：立即不再被召回，FR-MEM-05/08）；admin-only。
 *
 * 骨架先行：B2 的 X5（memory_admin_service）未交付时后端返回 **503 X5_NOT_DELIVERED**，
 * 页面显示「X5 未交付」横幅。MemoryView 形态未定稿，未知字段如实透传（AGENTS §4 禁区 9：
 * 不臆造字段），展示文本取 text/content 其一，均缺省时 JSON 串兜底。
 */

import { apiDelete, apiGet, apiPatch } from "./api";

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

/** X5 MemoryView 未定稿：已知口径字段可选，其余透传 */
export interface MemoryItem {
  chunk_id: string;
  text?: string;
  status?: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface MemoryListResult {
  items: MemoryItem[];
  limit: number;
  offset: number;
}

// ---------------------------------------------------------------------------
// 错误归一
// ---------------------------------------------------------------------------

export type MemoryErrorKind =
  | "not_delivered" // 503：X5 未交付
  | "forbidden" // 403：chunk 不属该长者（闸门②/AC-39，越权已审计）
  | "not_found" // 404：长者或记忆不存在
  | "server";

export class MemoryApiError extends Error {
  constructor(
    public readonly kind: MemoryErrorKind,
    message: string,
  ) {
    super(message);
    this.name = "MemoryApiError";
  }
}

async function readErrorDetail(res: Response): Promise<string> {
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  return body?.detail ?? "操作失败，请稍后重试";
}

/** 统一状态码分支；401 已由底层归一为 ApiError */
async function parseOrThrow<T>(res: Response): Promise<T> {
  if (res.ok) return (await res.json()) as T;
  const detail = await readErrorDetail(res);
  if (res.status === 503) {
    throw new MemoryApiError("not_delivered", "X5 未交付：记忆管理服务尚未部署");
  }
  if (res.status === 403) {
    throw new MemoryApiError("forbidden", "无权访问该长者的记忆（越权操作已记录审计）");
  }
  if (res.status === 404) {
    throw new MemoryApiError("not_found", "长者或记忆不存在");
  }
  throw new MemoryApiError("server", detail);
}

// ---------------------------------------------------------------------------
// 三个端点
// ---------------------------------------------------------------------------

/** 查看某长者记忆（隔离在 service 层强制 WHERE elder_id） */
export async function listMemories(
  elderId: string,
  opts: { limit?: number; offset?: number } = {},
): Promise<MemoryListResult> {
  const params = new URLSearchParams({
    limit: String(opts.limit ?? 50),
    offset: String(opts.offset ?? 0),
  });
  const res = await apiGet<MemoryListResult>(
    `/api/memory/${encodeURIComponent(elderId)}/memories?${params.toString()}`,
  );
  return res;
}

/** 纠正一条记忆（旧值 superseded、新值生效，FR-MEM-03/06） */
export async function correctMemory(
  elderId: string,
  chunkId: string,
  newText: string,
): Promise<MemoryItem> {
  const path = `/api/memory/${encodeURIComponent(elderId)}/memories/${encodeURIComponent(chunkId)}`;
  const res = await apiPatch(path, { new_text: newText });
  return parseOrThrow<MemoryItem>(res);
}

/** 删除一条记忆（立即不再被召回，FR-MEM-05/08） */
export async function deleteMemory(elderId: string, chunkId: string): Promise<void> {
  const path = `/api/memory/${encodeURIComponent(elderId)}/memories/${encodeURIComponent(chunkId)}`;
  const res = await apiDelete(path);
  if (!res.ok) {
    await parseOrThrow<unknown>(res); // 204/200 视为成功，其余按状态码分支抛错
  }
}

// ---------------------------------------------------------------------------
// 展示工具
// ---------------------------------------------------------------------------

/** 展示文本：text/content 其一，均缺省 JSON 兜底（X5 形态未定稿，不臆造字段） */
export function memoryItemText(m: MemoryItem): string {
  const content = m.text ?? (m as { content?: unknown }).content;
  if (typeof content === "string" && content.trim()) return content;
  return JSON.stringify(m);
}
