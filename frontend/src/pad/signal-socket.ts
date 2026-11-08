/**
 * T-E 信令 WS 状态机（C2 契约消费方）。
 * 状态：connecting → authing（open 后立即首发 auth，满足 300ms 死线）→ ready ⇄ connecting。
 * 心跳：20s 周期 ping；45s 无任何入站帧 → 主动 close 触发重连（与服务端判死口径一致）。
 * 重连：异常断开按 1/2/4/8/15s 退避（上限 15s）；4401 或 auth_fail 不重连，回调 onAuthFail。
 */
import {
  SIGNAL_WS_PATH,
  WS_CLOSE_UNAUTHORIZED,
  parseServerSignal,
  type ClientSignal,
  type ServerSignal,
} from "./protocol";

export type SocketState = "connecting" | "authing" | "ready" | "closed";

const PING_INTERVAL_MS = 20_000;
const DEAD_THRESHOLD_MS = 45_000;
const DEAD_CHECK_INTERVAL_MS = 5_000;
const BACKOFF_STEPS_MS = [1_000, 2_000, 4_000, 8_000, 15_000];
/** 连续失败达到该次数 → 停止自动重连，状态条出「点击重试」（adhoc §3.3） */
const MAX_AUTO_RETRIES = 5;

export class SignalSocket {
  private ws: WebSocket | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private deadTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFrameAt = 0;
  private backoffIndex = 0;
  private failCount = 0;
  private stopped = false;
  private wakeTs: number | undefined = undefined;

  onStateChange: ((state: SocketState) => void) | null = null;
  onServerSignal: ((msg: ServerSignal) => void) | null = null;
  /** 认证失效（4401 / auth_fail）：调用方应回登录页，不做退避重连 */
  onAuthFail: (() => void) | null = null;
  /** 连续 5 次自动重连失败：停止重连，由用户手动触发 retry() */
  onGiveUp: (() => void) | null = null;

  constructor(private token: string) {}

  /**
   * C-5：本轮 KWS 命中时刻（epoch 秒），随 auth 首帧上报（C2 契约 v1.5 可选字段）。
   * 消费一次即清：中断重连的 auth 不重发旧 wake_ts（时长会失真，服务端按 backend 兜底）。
   */
  setWakeTs(wakeTs: number): void {
    this.wakeTs = wakeTs;
  }

  connect(): void {
    if (this.stopped) return;
    // 先安全丢弃旧连接：摘掉回调避免其 onclose 再走一次退避重连（重复连接/泄漏），
    // 同时清掉挂起的重连定时器（retry 与到点重连并发时只保留本次）
    const old = this.ws;
    if (old) {
      old.onopen = old.onmessage = old.onclose = old.onerror = null;
      this.clearTimers();
      old.close();
      this.ws = null;
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.setState("connecting");
    const ws = new WebSocket(`${proto}://${location.host}${SIGNAL_WS_PATH}`);
    this.ws = ws;

    ws.onopen = () => {
      // C2：连接后 300ms 内必须首发 auth（同步发送，留足余量）
      // C-5：wake_ts 只随首个 auth 上报一次，重连 auth 不带（时长已失真，走 backend 兜底）
      const wakeTs = this.wakeTs;
      this.wakeTs = undefined;
      if (wakeTs === undefined) {
        this.send({ type: "auth", token: this.token });
      } else {
        this.send({ type: "auth", token: this.token, wake_ts: wakeTs });
      }
      this.setState("authing");
      this.lastFrameAt = Date.now();
      this.pingTimer = setInterval(() => this.send({ type: "ping" }), PING_INTERVAL_MS);
      this.deadTimer = setInterval(() => {
        if (Date.now() - this.lastFrameAt > DEAD_THRESHOLD_MS) ws.close();
      }, DEAD_CHECK_INTERVAL_MS);
    };

    ws.onmessage = (e) => {
      this.lastFrameAt = Date.now();
      const msg = parseServerSignal(typeof e.data === "string" ? e.data : "");
      if (!msg) return; // 非 JSON/type 未知：记 trace 后丢弃，不白屏
      if (msg.type === "auth_ok") {
        this.backoffIndex = 0; // 认证成功，重连退避复位
        this.failCount = 0;
        this.setState("ready");
      } else if (msg.type === "auth_fail") {
        this.stop();
        this.onAuthFail?.();
        return;
      }
      this.onServerSignal?.(msg);
    };

    ws.onclose = (e) => {
      this.clearTimers();
      if (this.stopped) return;
      if (e.code === WS_CLOSE_UNAUTHORIZED) {
        this.stop();
        this.onAuthFail?.();
        return;
      }
      // 异常断开 → 退避重连（±20% 随机抖动，防雪崩）
      const base = BACKOFF_STEPS_MS[Math.min(this.backoffIndex, BACKOFF_STEPS_MS.length - 1)];
      this.backoffIndex++;
      this.failCount++;
      if (this.failCount >= MAX_AUTO_RETRIES) {
        this.setState("closed");
        this.onGiveUp?.();
        return;
      }
      const jitter = base * (0.8 + Math.random() * 0.4);
      this.reconnectTimer = setTimeout(() => this.connect(), Math.round(jitter));
    };

    ws.onerror = () => {
      /* 统一走 onclose 的重连路径，不在 error 里处理 */
    };
  }

  send(msg: ClientSignal): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  get ready(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  /** 「点击重试」后调用：复位计数并重新连接 */
  retry(): void {
    this.failCount = 0;
    this.backoffIndex = 0;
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    this.ws?.close();
    this.ws = null;
    this.setState("closed");
  }

  private setState(state: SocketState): void {
    this.onStateChange?.(state);
  }

  private clearTimers(): void {
    if (this.pingTimer) clearInterval(this.pingTimer);
    if (this.deadTimer) clearInterval(this.deadTimer);
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.pingTimer = this.deadTimer = this.reconnectTimer = null;
  }
}
