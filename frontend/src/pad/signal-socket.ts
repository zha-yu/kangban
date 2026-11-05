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

export class SignalSocket {
  private ws: WebSocket | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private deadTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFrameAt = 0;
  private backoffIndex = 0;
  private stopped = false;

  onStateChange: ((state: SocketState) => void) | null = null;
  onServerSignal: ((msg: ServerSignal) => void) | null = null;
  /** 认证失效（4401 / auth_fail）：调用方应回登录页，不做退避重连 */
  onAuthFail: (() => void) | null = null;

  constructor(private token: string) {}

  connect(): void {
    if (this.stopped) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.setState("connecting");
    const ws = new WebSocket(`${proto}://${location.host}${SIGNAL_WS_PATH}`);
    this.ws = ws;

    ws.onopen = () => {
      // C2：连接后 300ms 内必须首发 auth（同步发送，留足余量）
      this.send({ type: "auth", token: this.token });
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
      // 异常断开 → 退避重连
      const delay = BACKOFF_STEPS_MS[Math.min(this.backoffIndex, BACKOFF_STEPS_MS.length - 1)];
      this.backoffIndex++;
      this.reconnectTimer = setTimeout(() => this.connect(), delay);
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
