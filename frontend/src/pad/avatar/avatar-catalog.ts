/**
 * 本地 VRM 资产目录（Day05 A1 S2，方案 docs/plans/day05/a1-本地VRM与形象选择页.md §4.2）
 *
 * - 模型来源为主理人裁决的本地资产（frontend/public/assets/vrm/，不入库），
 *   不再消费 C7 `GET /api/pad/avatar-config`（留痕见 docs/team_tasks/day05/a1-本地VRM与形象选择页.md）
 * - 14 个模型均为 VRM0（spike 实测），rotationYOffset 需逐个标定，S3 验证时补值
 * - lipsync 按主理人裁决（R8-b）：无表情模型标记 "none"，选择页展示「不支持口型」标注
 */

/** 口型驱动方式：expression=aa 表情驱动（默认）；none=模型无表情，不支持口型（R8-b） */
export type LipsyncMode = "expression" | "none";

export interface AvatarCatalogItem {
  /** 稳定标识，localStorage 只存这个值，不用文件名 */
  id: string;
  /** 选择页大字号展示名 */
  displayName: string;
  /** 模型文件相对 public 的路径 */
  url: string;
  /** 文件体积（MB，选择页标注供长者感知等待） */
  sizeMB: number;
  /** VRM0 朝向补偿（弧度）。全部待 S3 逐个标定后写死 */
  rotationYOffset: number;
  /** 口型能力（R8-b：Danheng/Evelyn/Firefly/Neuvillette 实测 0 表情） */
  lipsync: LipsyncMode;
}

export interface AnimationCatalogItem {
  id: string;
  displayName: string;
  url: string;
  /** idle=待机循环叠加；action=指定触发动作（预览/后续扩展） */
  kind: "idle" | "action";
}

/** 模型清单：frontend/public/assets/vrm/ 下 14 个 .vrm（体积为 ls 实测） */
export const AVATAR_CATALOG: readonly AvatarCatalogItem[] = [
  { id: "yoimiya",     displayName: "宵宫",     url: "/assets/vrm/Yoimiya.vrm",     sizeMB: 5.7,  rotationYOffset: 0, lipsync: "expression" },
  { id: "neuvillette", displayName: "那维莱特", url: "/assets/vrm/Neuvillette.vrm", sizeMB: 6.3,  rotationYOffset: 0, lipsync: "none" },
  { id: "danheng",     displayName: "丹恒",     url: "/assets/vrm/Danheng.vrm",     sizeMB: 7.8,  rotationYOffset: 0, lipsync: "none" },
  { id: "fischl",      displayName: "菲谢尔",   url: "/assets/vrm/Fischl.vrm",      sizeMB: 7.8,  rotationYOffset: 0, lipsync: "expression" },
  { id: "linghua",     displayName: "绫华",     url: "/assets/vrm/Linghua.vrm",     sizeMB: 10,   rotationYOffset: 0, lipsync: "expression" },
  { id: "jizi",        displayName: "姬子",     url: "/assets/vrm/Jizi.vrm",        sizeMB: 11,   rotationYOffset: 0, lipsync: "expression" },
  { id: "ningguang",   displayName: "凝光",     url: "/assets/vrm/Ningguang.vrm",   sizeMB: 11,   rotationYOffset: 0, lipsync: "expression" },
  { id: "shenhe",      displayName: "申鹤",     url: "/assets/vrm/Shenhe.vrm",      sizeMB: 12,   rotationYOffset: 0, lipsync: "expression" },
  { id: "sparkle",     displayName: "花火",     url: "/assets/vrm/Sparkle.vrm",     sizeMB: 12,   rotationYOffset: 0, lipsync: "expression" },
  { id: "jingyuan",    displayName: "景元",     url: "/assets/vrm/Jingyuan.vrm",    sizeMB: 13,   rotationYOffset: 0, lipsync: "expression" },
  { id: "firefly",     displayName: "流萤",     url: "/assets/vrm/Firefly.vrm",     sizeMB: 14,   rotationYOffset: 0, lipsync: "none" },
  { id: "lisa",        displayName: "丽莎",     url: "/assets/vrm/Lisa.vrm",        sizeMB: 16,   rotationYOffset: 0, lipsync: "expression" },
  { id: "evelyn",      displayName: "伊芙琳",   url: "/assets/vrm/Evelyn.vrm",      sizeMB: 34,   rotationYOffset: 0, lipsync: "none" },
  { id: "yinlin",      displayName: "吟霖",     url: "/assets/vrm/Yinlin.vrm",      sizeMB: 40,   rotationYOffset: 0, lipsync: "expression" },
] as const;

/** 动画白名单（spike 结论：common/ 20 个精选均可用；world-* 为世界系动画，不适合本舞台口径，不收录） */
export const ANIMATION_CATALOG: readonly AnimationCatalogItem[] = [
  { id: "idle",         displayName: "自然待机",   url: "/assets/vrm/animations/common/idle.vrma",         kind: "idle" },
  { id: "idle-talking", displayName: "站立说话",   url: "/assets/vrm/animations/common/idle-talking.vrma", kind: "action" },
  { id: "nod",          displayName: "点头",       url: "/assets/vrm/animations/common/nod.vrma",          kind: "action" },
  { id: "happy",        displayName: "鼓掌",       url: "/assets/vrm/animations/common/happy.vrma",        kind: "action" },
  { id: "raise-hand",   displayName: "举手",       url: "/assets/vrm/animations/common/raise-hand.vrma",   kind: "action" },
  { id: "think",        displayName: "环顾思考",   url: "/assets/vrm/animations/common/think.vrma",        kind: "action" },
] as const;

/** 出厂默认模型（Q1 裁决：体积最小的 Yoimiya） */
export const DEFAULT_AVATAR_ID = "yoimiya";

/** 模型选择的 localStorage 键（按设备持久化，pad 为设备绑定唯一长者） */
export const AVATAR_STORAGE_KEY = "pad.avatar.selected";

/** 按 id 查清单项，未命中返回 undefined */
export function findAvatarById(id: string | null | undefined): AvatarCatalogItem | undefined {
  return AVATAR_CATALOG.find((item) => item.id === id);
}

/**
 * 解析应展示的模型：localStorage 值合法则用之，否则回落出厂默认。
 * 兜底 DEFAULT 项意外缺失时返回清单首项（结构性兜底，正常不触发）。
 */
export function resolveSelectedAvatar(storedId: string | null | undefined): AvatarCatalogItem {
  return (
    findAvatarById(storedId) ??
    findAvatarById(DEFAULT_AVATAR_ID) ??
    AVATAR_CATALOG[0]
  );
}

/** 读取 localStorage 中已保存的选择（仅读取，不校验合法性；合法性由 resolveSelectedAvatar 处理） */
export function readStoredAvatarId(): string | null {
  try {
    return localStorage.getItem(AVATAR_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** 写入选择（失败静默，不影响主流程） */
export function writeStoredAvatarId(id: string): void {
  try {
    localStorage.setItem(AVATAR_STORAGE_KEY, id);
  } catch {
    /* 隐私模式等场景写入失败时保持默认 */
  }
}
