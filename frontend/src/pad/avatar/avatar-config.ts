/**
 * 【已停用 2026-11-07，Day05 A1 S4】本文件不再被任何代码引用——数字人形象改为本地资产驱动
 * （avatar-catalog.ts + AvatarStage.tsx，D1 裁决），此处仅保留接口契约本体备查，禁止新调用。
 * 留痕：docs/team_tasks/day05/a1-本地VRM与形象选择页.md（S0）；后端接口未删除。
 *
 * C7 avatar-config REST 契约消费方（冻结人 A3；docs/team_tasks/day04/contracts.md C7，v1.1 缓存口径）。
 * GET /api/pad/avatar-config，Authorization: Bearer <device access_token>；elder_id 一律服务端解出。
 * 错误三路：401 → onAuthFail（走既有回登录逻辑）；403 device_not_bound → 抛 AvatarConfigError
 * 由 UI 明示（需人工到照护台重绑，不能糊成「没配形象」）；网络/其他失败 → 返回 null
 * （前端走内置兜底，不白屏，C7 语义约定的取值链第三层）。
 */

/** C7 200 响应体：四键严格为这四项 */
export interface AvatarConfig {
  /** 绝对 URL；null → 前端直接用内置兜底 */
  avatar_url: string | null;
  /** 静态头像（VRM 降级用） */
  fallback_image_url: string | null;
  /** VRM0 朝向补偿（弧度），VRM1 恒 0.0（C7） */
  rotation_y_offset: number;
  /**
   * 不透明缓存键（C7 v1.1 裁决）：实现为内容指纹、可回退，仅作缓存键；
   * 正确用法是「值变了就重取」，禁止做新旧大小比较。
   */
  version: number;
}

/** C7 403：设备未绑定长者，需人工处置，与「后端没配形象」是两种不同的失败 */
export class AvatarConfigError extends Error {
  constructor(
    public readonly code: "device_not_bound",
    message: string,
  ) {
    super(message);
  }
}

const CONFIG_TIMEOUT_MS = 5_000;

/**
 * 拉取数字人形象配置。任何网络/解析失败返回 null（调用方走内置兜底），
 * 只有 device_not_bound 以异常形式抛出（UI 需明示）。
 */
export async function fetchAvatarConfig(
  token: string,
  onAuthFail: () => void,
): Promise<AvatarConfig | null> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), CONFIG_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch("/api/pad/avatar-config", {
      headers: { Authorization: `Bearer ${token}` },
      signal: ctrl.signal,
    });
  } catch {
    return null; // 网络失败 / 超时 → 内置兜底
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401) {
    // 设备会话过期：沿用主链路同一套回登录逻辑（T-H 方案 A）
    onAuthFail();
    return null;
  }
  if (res.status === 403) {
    // C7 错误体 {detail, code, trace_id}；此处只需 code，不透出内部 detail
    throw new AvatarConfigError("device_not_bound", "设备还没有绑定长者，请在照护台完成绑定");
  }
  if (!res.ok) return null;

  let body: unknown;
  try {
    body = await res.json();
  } catch {
    return null;
  }
  const cfg = body as Partial<AvatarConfig> | null;
  if (cfg === null || typeof cfg !== "object" || typeof cfg.version !== "number") return null;
  return {
    avatar_url: typeof cfg.avatar_url === "string" ? cfg.avatar_url : null,
    fallback_image_url: typeof cfg.fallback_image_url === "string" ? cfg.fallback_image_url : null,
    rotation_y_offset: typeof cfg.rotation_y_offset === "number" ? cfg.rotation_y_offset : 0,
    version: cfg.version,
  };
}
