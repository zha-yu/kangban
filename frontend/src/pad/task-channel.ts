/**
 * pad 任务下发契约（前端侧，v1.5）
 *
 * 背景：PRD §2.2 / FR-UI-10 / FR-MED-06 把「反向 WebRTC 外呼」定为主动触达主路径，
 * 外呼建连失败时降级为「WebSocket 下发页面播报」；§4.6 的健康采集由 19:00 定时
 * 或工作人员补启动，FR-VPR-02 的身份确认由声纹低置信触发。当前 RealtimeClient
 * 只在点呼叫 / 唤醒词命中时建连，**尚无自动接听外呼的能力**，主路径未落地。
 *
 * 因此本模块不绑定任何传输：前端只消费「任务命令」，由未来的适配器把不同来源
 * 翻译成同一条命令。真实来源（Day 8 起）：信令 WS 下行 task_command 帧
 * （kind=medication|health，day08/contracts.md §C-1）+ 开发态切换条（source="dev"）。
 *
 * 已接适配器：
 *  1. C2 信令 WS task_command 下行帧 → App.tsx onTaskCommand → emitPadTask(open)；
 *     用药（Day 7 L6#9）与健康采集（Day 8 §C-1）同帧类型不同 kind。
 *
 * 仍待接（需先冻结后端契约）：
 *  2. REST 轮询 GET /api/pad/tasks → 拉到新任务 emitPadTask(open)。延迟与常驻轮询
 *     成本都高于 WS，仅作离线补单兜底，不作为首选（离线补推已由后端调度 tick 覆盖）。
 *  3. 反向外呼 auth_ok 携带 task 扩展 → 与 1 同属 C2 契约，等主路径落地后合并。
 *
 * 回程（答案提交与渲染回执已接）：健康采集答案 → POST /api/pad/health-records
 * （§P-1，health-client.ts）；卡片渲染成功 → client_receipt/card_rendered 帧经信令 WS
 * 上行（Day 9 X1，realtime.sendClientReceipt；standby 期无 WS 会话时丢弃，联调走会话内路径）。
 */
import {
  MOCK_CANDIDATES,
  MOCK_MEDICATION,
  type IdentityCandidate,
  type MedicationSnapshot,
  type TaskKind,
} from "./tasks";

/** 可由服务端下发到主屏的三类任务面板；SOS 是常显按钮，不是下行任务 */
export type PadTaskKind = Exclude<TaskKind, "none" | "sos">;

/** 触发来源。前端只用于展示与去重，不改变面板行为 */
export type PadTaskSource =
  | "outbound_call" // FR-UI-10 反向 WebRTC 外呼（主路径，未落地）
  | "ws_fallback" // FR-MED-06 外呼失败降级 WS 页面播报（建议首选接入）
  | "scheduler" // 19:00 健康采集定时触发
  | "staff" // 护工补启动
  | "voiceprint" // 声纹低置信 / 服务不可用
  | "dev"; // 开发态切换条（当前唯一真实来源）

/** 任务载荷：形状按 PRD 字段预留，后端契约冻结前一律由 dev mock 填充 */
export type PadTaskPayload =
  | { kind: "medication"; snapshot: MedicationSnapshot }
  | { kind: "health"; startedAt: string; source: "voice_scheduled" | "staff_scheduled" }
  | {
      kind: "identity";
      candidates: IdentityCandidate[];
      reason: "low_confidence" | "service_unavailable";
    };

export interface PadTaskCommand {
  /** 幂等键：同一 taskId 重复 open 不重启面板（对应 FR-MED-04 触达键口径） */
  taskId: string;
  kind: PadTaskKind;
  source: PadTaskSource;
  /** 长者可读的界面提示，进大字幕 notice 区。**不是 TTS 文本**，不进语音路由 */
  hint: string;
  payload?: PadTaskPayload;
  issuedAt?: number;
  /** Day 9 X1：触达通知行 id（服务端 task_command.notification_id 透传）；
   *  card_rendered 回执必带，纯播报/开发态为 null。 */
  notificationId?: string | null;
}

export type PadTaskEvent =
  | { type: "open"; command: PadTaskCommand }
  | {
      type: "close";
      taskId: string;
      reason: "completed" | "timeout" | "user_exit" | "superseded";
    };

const PAD_TASK_EVENT = "kangban:pad-task";

/** 到达提示：原型期没有 TTS，统一走 notice，避免把 UI 文字伪装成数字人语音 */
export const TASK_HINTS: Record<PadTaskKind, string> = {
  medication: "该吃药了，请看右边的用药提醒",
  health: "今天的健康采集开始了，请回答右边的问题",
  identity: "还没听出是谁在说话，请点一下您的名字",
};

/** 唯一写入口。适配器（WS / REST / 外呼 / 开发切换条）都调这里 */
export function emitPadTask(event: PadTaskEvent): void {
  window.dispatchEvent(new CustomEvent(PAD_TASK_EVENT, { detail: event }));
}

/** 唯一读入口。返回取消订阅函数，供 MainScreen 的 effect 清理 */
export function subscribePadTasks(listener: (event: PadTaskEvent) => void): () => void {
  const handler = (e: Event): void => {
    const detail = (e as CustomEvent<unknown>).detail;
    if (!detail || typeof detail !== "object") return;
    const type = (detail as { type?: unknown }).type;
    if (type === "open" || type === "close") listener(detail as PadTaskEvent);
  };
  window.addEventListener(PAD_TASK_EVENT, handler);
  return () => window.removeEventListener(PAD_TASK_EVENT, handler);
}

/** 开发态适配器：切换条按下时造一条与后端同形的命令，走同一条总线 */
export function makeDevTask(kind: PadTaskKind): PadTaskCommand {
  const payload: PadTaskPayload =
    kind === "medication"
      ? { kind: "medication", snapshot: MOCK_MEDICATION }
      : kind === "health"
        ? { kind: "health", startedAt: "19:00", source: "voice_scheduled" }
        : {
            kind: "identity",
            candidates: MOCK_CANDIDATES,
            reason: "service_unavailable",
          };
  return {
    taskId: `dev-${kind}`,
    kind,
    source: "dev",
    hint: TASK_HINTS[kind],
    payload,
    issuedAt: Date.now(),
  };
}

/** 开发验收直达：?demo=medication|health|identity，仅 DEV 生效 */
export function readDemoTask(): PadTaskKind | null {
  if (!import.meta.env.DEV) return null;
  const demo = new URLSearchParams(window.location.search).get("demo");
  return demo === "medication" || demo === "health" || demo === "identity"
    ? demo
    : null;
}
