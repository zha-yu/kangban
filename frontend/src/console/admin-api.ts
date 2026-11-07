/**
 * 管理员工 / 家属账号 API 封装（Day 5 · D-1 / N1）。
 *
 * 契约唯一依据：docs/接口契约_身份与权限.md v1.8 §4 E12 / E15 / E13、§7 错误码。
 * 只封装 console 尚无封装的三个端点；E18/E16/E17/E19/E5/E9 一律复用
 * `elders-api.ts` 既有函数（AGENTS §4 禁区 8：不新增第二套同类封装）。
 *
 * 403/409/404/422 处置走主计划 K2 方案 B：本模块内解析状态码并抛页面级文案，
 * `console/api.ts` 零改动（不新增 forbidden kind，不回归登录页错误分支）。
 * 错误文案用用户语言，不回显 trace_id、detail 原文或内部实现细节。
 */

import { ApiError, NETWORK_ERROR_TEXT, apiPost } from "./api";

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

/** 409 文案随业务场景不同，由调用方给出；其余状态码统一文案 */
function messageForStatus(status: number, conflictText: string): { kind: AdminApiErrorKind; message: string } {
  switch (status) {
    case 403:
      return { kind: "forbidden", message: "当前账号无权执行该操作" };
    case 409:
      return { kind: "conflict", message: conflictText };
    case 404:
      return { kind: "not_found", message: "目标不存在，请刷新列表后重试" };
    case 422:
      return { kind: "validation", message: "提交内容不符合要求，请按提示检查后重试" };
    case 400:
      return { kind: "bad_request", message: "请求格式有误，请刷新页面后重试" };
    default:
      return { kind: "network", message: "服务暂时不可用，请稍后重试" };
  }
}

/** 统一 POST：网络错 / 401 归一为 AdminApiError，其余状态码按契约 §7 分支 */
async function adminPost<T>(path: string, body: unknown, conflictText: string): Promise<T> {
  let res: Response;
  try {
    res = await apiPost(path, body);
  } catch (err) {
    if (err instanceof ApiError) {
      throw new AdminApiError(err.kind === "invalid" ? "invalid" : "network", err.message);
    }
    throw new AdminApiError("network", NETWORK_ERROR_TEXT);
  }
  if (!res.ok) {
    const mapped = messageForStatus(res.status, conflictText);
    throw new AdminApiError(mapped.kind, mapped.message);
  }
  return (await res.json()) as T;
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
