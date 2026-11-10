/**
 * 管理员工 / 家属账号 API 封装（Day 5 · D-1 / N1；Day 6 · N18 扩展）。
 *
 * 契约唯一依据：docs/接口契约_身份与权限.md v1.8 §4 E12 / E15 / E13、§7 错误码；
 * Day 6 E20 改绑：docs/team_tasks/day06/contracts.md v1.0 §6（已冻结）。
 * `listStaff`（GET /api/staff）为 Day 6 契约增补（主理人 2026-11-09 裁决，dev-B 已落地：
 * app/api/staff.py `list_staff`）——contracts.md 尚未补登该节，J6 对齐时请 dev-B 补书面口径。
 * E18/E16/E17/E19/E5/E9 一律复用 `elders-api.ts` 既有函数
 * （AGENTS §4 禁区 8：不新增第二套同类封装）。
 *
 * 403/409/404/422 处置走主计划 K2 方案 B：本模块内解析状态码并抛页面级文案，
 * `console/api.ts` 零改动（不新增 forbidden kind，不回归登录页错误分支）。
 * 错误文案用用户语言，不回显 trace_id、detail 原文或内部实现细节。
 *
 * Day 7（D2，P2-26）：401 不再透传登录页「手机号或密码不正确」——管理页收到 401
 * 意味着登录态过期（authedFetch 内 refresh 已失败），统一「登录已过期，请重新登录」；
 * 400/422 解析响应体 detail 提示具体原因，不再统一「请刷新页面」。
 */

import { ApiError, NETWORK_ERROR_TEXT, apiGetWithStatus, apiPost, apiPut } from "./api";

export type StaffRole = "admin" | "care_worker" | "doctor";
export type AccountStatus = "active" | "disabled";

/** E12 建档请求：`elder_ids` 仅对 care_worker / doctor 有意义，admin 传非空会被 403 */
export interface StaffCreateInput {
  name: string;
  role: StaffRole;
  phone: string;
  password: string;
  elder_ids?: string[];
}

/** E12 响应：只回脱敏手机号，不含口令哈希与完整号码 */
export interface StaffCreated {
  staff_id: string;
  name: string;
  role: StaffRole;
  status: AccountStatus;
  phone_masked: string;
}

/** E15 建家属账号请求 */
export interface GuardianCreateInput {
  name: string;
  phone: string;
  password: string;
}

/** E15 响应：建号与绑定是两步，先拿 guardian_id 再走 E13 */
export interface GuardianCreated {
  guardian_id: string;
  name: string;
  status: AccountStatus;
  phone_masked: string;
}

/** E13 绑定结果 */
export interface GuardianBinding {
  elder_id: string;
  guardian_id: string;
  relation: string;
  status: "active" | "unbound";
}

/** 页面级错误类别（K2-B：只在 admin 页消费，不并入 ApiError） */
export type AdminApiErrorKind =
  | "invalid" // 401：登录态失效，需重新登录
  | "forbidden" // 403：角色不足 / 范围外
  | "conflict" // 409：手机号占用、重复绑定、超过 2 位监护人
  | "not_found" // 404：员工 / 长者 / 家属不存在
  | "validation" // 422：字段格式不合法
  | "bad_request" // 400：请求格式坏
  | "network"; // 网络异常 / 5xx

export class AdminApiError extends Error {
  constructor(
    public readonly kind: AdminApiErrorKind,
    message: string,
  ) {
    super(message);
    this.name = "AdminApiError";
  }
}

/** 401 文案（P2-26）：登录态过期（refresh 已失败），引导重新登录，不提示「密码不正确」 */
const SESSION_EXPIRED_TEXT = "登录已过期，请重新登录";

/** 400/422 响应体 detail 解析（FastAPI 风格）：字符串直接用；pydantic 数组取首条 msg。
 *  解析失败（非 JSON / 空）返回 null，回落到通用文案。 */
async function detailOf(res: Response): Promise<string | null> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body?.detail === "string" && body.detail.trim()) return body.detail.trim();
    if (Array.isArray(body?.detail) && body.detail.length > 0) {
      const first = body.detail[0] as { msg?: unknown };
      if (typeof first?.msg === "string" && first.msg.trim()) return first.msg.trim();
    }
  } catch {
    // 响应体非 JSON 或已读，忽略走通用文案
  }
  return null;
}

/** 409/403 文案随业务场景不同，由调用方给出（缺省用通用文案）；其余状态码统一文案 */
function messageForStatus(
  status: number,
  conflictText: string,
  forbiddenText = "当前账号无权执行该操作",
  detail: string | null = null,
): { kind: AdminApiErrorKind; message: string } {
  switch (status) {
    case 403:
      return { kind: "forbidden", message: forbiddenText };
    case 409:
      return { kind: "conflict", message: conflictText };
    case 404:
      return { kind: "not_found", message: "目标不存在，请刷新列表后重试" };
    case 422:
      return {
        kind: "validation",
        message: detail ? `提交内容不符合要求：${detail}` : "提交内容不符合要求，请按提示检查后重试",
      };
    case 400:
      return { kind: "bad_request", message: detail ? `请求有误：${detail}` : "请求格式有误，请检查后重试" };
    default:
      return { kind: "network", message: "服务暂时不可用，请稍后重试" };
  }
}

/** 统一请求发送：网络错 / 401 归一为 AdminApiError，其余状态码按契约 §7 分支（POST/PUT 共用） */
async function adminSend<T>(
  doSend: () => Promise<Response>,
  conflictText: string,
  forbiddenText?: string,
): Promise<T> {
  let res: Response;
  try {
    res = await doSend();
  } catch (err) {
    if (err instanceof ApiError) {
      // P2-26：401（refresh 已失败）引导重新登录，不透传登录页「手机号或密码不正确」
      throw new AdminApiError(
        err.kind === "invalid" ? "invalid" : "network",
        err.kind === "invalid" ? SESSION_EXPIRED_TEXT : err.message,
      );
    }
    throw new AdminApiError("network", NETWORK_ERROR_TEXT);
  }
  if (!res.ok) {
    // apiPost/apiPut 已在 401 抛 ApiError，这里兜底防御；400/422 解析 detail 提示具体原因
    if (res.status === 401) {
      throw new AdminApiError("invalid", SESSION_EXPIRED_TEXT);
    }
    const detail = res.status === 400 || res.status === 422 ? await detailOf(res) : null;
    const mapped = messageForStatus(res.status, conflictText, forbiddenText, detail);
    throw new AdminApiError(mapped.kind, mapped.message);
  }
  return (await res.json()) as T;
}

/** 统一 POST：网络错 / 401 归一为 AdminApiError，其余状态码按契约 §7 分支 */
async function adminPost<T>(path: string, body: unknown, conflictText: string): Promise<T> {
  return adminSend<T>(() => apiPost(path, body), conflictText);
}

/** E12 POST /api/staff —— 建员工账号（仅 admin） */
export function createStaff(input: StaffCreateInput): Promise<StaffCreated> {
  return adminPost<StaffCreated>("/api/staff", input, "该手机号已被占用，请换一个号码");
}

/** E15 POST /api/guardians —— 建家属账号（仅 admin） */
export function createGuardian(input: GuardianCreateInput): Promise<GuardianCreated> {
  return adminPost<GuardianCreated>("/api/guardians", input, "该手机号已被占用，请换一个号码");
}

/** E13 POST /api/elders/{elder_id}/guardians —— 把已有家属绑定到长者（仅 admin，≤2 位） */
export function bindGuardian(elderId: string, guardianId: string, relation: string): Promise<GuardianBinding> {
  return adminPost<GuardianBinding>(
    `/api/elders/${encodeURIComponent(elderId)}/guardians`,
    { guardian_id: guardianId, relation },
    "该家属已绑定此长者，或该长者已满 2 位监护人",
  );
}

/** E20 PUT /api/staff/{staff_id}/elders —— 全量覆盖式替换员工负责长者（仅 admin）。
 *  契约：day06 contracts.md v1.0 §6。200 返回差集；409=移除后某长者失去全部 primary_care；
 *  前端不做「是否最后一位责任护工」本地预判，规则单点在后端（避免两处漂移）。 */
export interface ReassignResult {
  staff_id: string;
  added: string[];
  removed: string[];
}

export function reassignElders(staffId: string, elderIds: string[]): Promise<ReassignResult> {
  return adminSend<ReassignResult>(
    () => apiPut(`/api/staff/${encodeURIComponent(staffId)}/elders`, { elder_ids: elderIds }),
    "该长者将失去最后一位责任护工，请先指定接替人",
    "只有管理员可以调整负责关系",
  );
}

/** GET /api/staff 列表项（dev-B 已落地实现为准）：与后端 `StaffListItemResponse` 字段名零差异。 */
export interface StaffListItem {
  staff_id: string;
  name: string;
  role: StaffRole;
  status: AccountStatus;
  phone_masked: string;
  /** 当前负责长者（care_worker/doctor 有值）；admin 恒为空数组 */
  bound_elder_ids: string[];
}

export async function listStaff(): Promise<StaffListItem[]> {
  let res: { status: number; body: { items: StaffListItem[] } | null };
  try {
    res = await apiGetWithStatus<{ items: StaffListItem[] }>("/api/staff");
  } catch (err) {
    if (err instanceof ApiError) {
      // P2-26：401（refresh 已失败）引导重新登录，不透传登录页「手机号或密码不正确」
      throw new AdminApiError(
        err.kind === "invalid" ? "invalid" : "network",
        err.kind === "invalid" ? SESSION_EXPIRED_TEXT : err.message,
      );
    }
    throw new AdminApiError("network", NETWORK_ERROR_TEXT);
  }
  if (res.body !== null) return res.body.items;
  if (res.status === 403) {
    throw new AdminApiError("forbidden", "当前账号无权执行该操作");
  }
  throw new AdminApiError("network", "服务暂时不可用，请稍后重试");
}
