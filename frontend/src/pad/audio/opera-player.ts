/**
 * pad 戏曲播放器（Day 8 · D4）。
 *
 * ## 它管什么
 *
 * 照护台契约 §6.3 冻结的**七动作**：pause / resume / stop / next_track /
 * volume_up / volume_down / replay。「打断」不在七个动作里——它是 WebRTC 语音
 * 链路的 barge-in 自然行为（FR-CON-03），由 `duckForSpeech()` 承担。
 *
 * ## 它不管什么
 *
 * - **不取数据**：`media_url` 由调用方给（后端 `media_service.get_playback_url`
 *   产出的稳定路径），本类只负责播——**不感知扩展名**：浏览器 `<audio>` 对
 *   wav/mp3 一视同仁，所以 2026-11-11 B4 把素材从 wav 换成 mp3 时本类零改动。
 *   （契约 §6.1 目前仍写死 `.wav`，扩展名口径待 dev-B 在 §6 留痕，见 J-X8 追加。）
 * - **不写 history**：`media_history` 归 C4 `media_service`，pad 与 Tool 都不写
 *   （契约 §6.2 末句）。
 * - **不认 WS 帧**：帧形状未定（Q-D4），所以这里没有 `protocol.ts` 依赖。等 A 定
 *   形状、B 冻结后，把 `apply(action)` 接到帧处理里即可，本类不用改。
 *
 * ## 状态机
 *
 * ```
 * idle ──load()──▶ loading ──canplay──▶ playing ⇄ paused
 *   ▲                  │                  │        │
 *   └────stop()────────┴──────────────────┴────────┘
 *                                    ended ──replay()──▶ playing
 * ```
 *
 * 非法动作**不抛异常也不静默**：返回 `ignored` 并说明原因，调用方可打诊断。
 * 这样「说话时按暂停」「没在播时按继续」这类误触不会把界面卡死。
 */

/** 契约 §6.3 的七动作，一字不多一字不少 */
export type OperaAction =
  | "pause"
  | "resume"
  | "stop"
  | "next_track"
  | "volume_up"
  | "volume_down"
  | "replay";

export type OperaStatus = "idle" | "loading" | "playing" | "paused" | "ended";

export interface OperaTrack {
  /** `media_library.media_id`（uuid 字符串） */
  mediaId: string;
  title: string;
  /** X8 稳定路径；后端换素材时 URL 不变 */
  mediaUrl: string;
  /** 秒；仅用于展示与进度，不参与判定 */
  durationSec?: number;
}

export interface OperaSnapshot {
  status: OperaStatus;
  track: OperaTrack | null;
  /** 0–1；0 = 静音 */
  volume: number;
  /** 当前播放位置（秒），idle 时为 0 */
  positionSec: number;
  /** 因播报被压住而暂存「回来后要继续」 */
  ducked: boolean;
}

/** 动作结果：`ok` 已执行；`ignored` 当前状态不适用；`needs_track` 换段需外部先取段 */
export type OperaActionResult = "ok" | "ignored" | "needs_track";

export interface OperaPlayerDeps {
  /** 注入点：单测可传假件；默认 `new Audio()` */
  createAudio?: () => HTMLAudioElement;
  /** 音量步长，默认 0.2（5 档） */
  volumeStep?: number;
  /** 状态变化回调（UI 订阅） */
  onState?: (snapshot: OperaSnapshot) => void;
  /** 失败回调（加载失败/播放被拒），只上报不抛 */
  onError?: (reason: string, detail?: unknown) => void;
}

const DEFAULT_VOLUME = 0.8;

export class OperaPlayer {
  private audio: HTMLAudioElement | null = null;
  private status: OperaStatus = "idle";
  private track: OperaTrack | null = null;
  private volume = DEFAULT_VOLUME;
  private ducked = false;
  /** 被 speech 压住前是否在播：只有它才允许 resumeAfterSpeech 回放 */
  private resumeAfterDuck = false;
  private disposed = false;

  constructor(private readonly deps: OperaPlayerDeps = {}) {}

  // ------------------------------------------------------------------ 状态

  get state(): OperaSnapshot {
    return {
      status: this.status,
      track: this.track,
      volume: this.volume,
      positionSec: this.audio?.currentTime ?? 0,
      ducked: this.ducked,
    };
  }

  private emit(): void {
    this.deps.onState?.(this.state);
  }

  private setStatus(next: OperaStatus): void {
    if (this.status === next) return;
    this.status = next;
    this.emit();
  }

  private ensureAudio(): HTMLAudioElement {
    if (this.audio) return this.audio;
    const el = this.deps.createAudio ? this.deps.createAudio() : new Audio();
    el.preload = "auto";
    el.volume = this.volume;
    el.addEventListener("playing", () => this.setStatus("playing"));
    el.addEventListener("pause", () => {
      // ended 也会触发 pause，不能把 ended 覆盖成 paused
      if (this.status === "ended" || this.disposed) return;
      this.setStatus(this.ducked ? "paused" : "paused");
    });
    el.addEventListener("ended", () => this.setStatus("ended"));
    el.addEventListener("error", () => {
      this.deps.onError?.("media_error", el.error);
      this.setStatus("idle");
    });
    this.audio = el;
    return el;
  }

  // ------------------------------------------------------------------ 播放

  /**
   * 装载并起播一段。同一时刻只有一个音源：新曲顶掉旧曲（不叠加）。
   * 起播被浏览器拒绝（无用户手势）不抛，回调 `onError("play_rejected")` 并停在 paused。
   */
  async load(track: OperaTrack): Promise<OperaActionResult> {
    if (this.disposed) return "ignored";
    const el = this.ensureAudio();
    const sameTrackPlaying =
      this.track?.mediaId === track.mediaId && (this.status === "playing" || this.status === "loading");
    if (sameTrackPlaying) return "ignored"; // 已经在放同一段，不打断

    this.track = track;
    this.ducked = false;
    this.resumeAfterDuck = false;
    el.src = track.mediaUrl;
    el.volume = this.volume;
    this.setStatus("loading");
    try {
      await el.play();
      return "ok";
    } catch (err) {
      // 自动播放被拒（无手势）不是错误状态，停在 paused 等用户点一下
      this.deps.onError?.("play_rejected", err);
      this.setStatus("paused");
      return "ignored";
    }
  }

  /**
   * 执行契约七动作之一。
   *
   * `next_track` 需要调用方先取好下一段（`media_service` 在服务端），所以这里
   * 只接受 `nextTrack` 参数；没给就返回 `needs_track`，由调用方去取段——本类
   * **不猜**「下一段是谁」。
   */
  async apply(action: OperaAction, nextTrack?: OperaTrack): Promise<OperaActionResult> {
    if (this.disposed) return "ignored";
    const el = this.audio;
    switch (action) {
      case "pause":
        if (!el || this.status !== "playing") return "ignored";
        el.pause();
        this.setStatus("paused");
        return "ok";

      case "resume":
        if (!el || this.status !== "paused" || this.ducked) return "ignored";
        try {
          await el.play();
          return "ok";
        } catch (err) {
          this.deps.onError?.("play_rejected", err);
          return "ignored";
        }

      case "stop":
        if (!el || this.status === "idle") return "ignored";
        el.pause();
        el.currentTime = 0;
        this.track = null;
        this.ducked = false;
        this.resumeAfterDuck = false;
        this.setStatus("idle");
        return "ok";

      case "next_track":
        if (!nextTrack) return "needs_track";
        return await this.load(nextTrack);

      case "volume_up":
        return this.setVolume(this.volume + this.step());

      case "volume_down":
        return this.setVolume(this.volume - this.step());

      case "replay":
        if (!el || !this.track) return "ignored";
        el.currentTime = 0;
        try {
          await el.play();
          return "ok";
        } catch (err) {
          this.deps.onError?.("play_rejected", err);
          return "ignored";
        }

      default:
        return "ignored";
    }
  }

  private step(): number {
    return this.deps.volumeStep ?? 0.2;
  }

  private setVolume(next: number): OperaActionResult {
    const clamped = Math.min(1, Math.max(0, Number(next.toFixed(2))));
    if (clamped === this.volume) return "ignored";
    this.volume = clamped;
    if (this.audio) this.audio.volume = clamped;
    this.emit();
    return "ok";
  }

  // ------------------------------------------------------- 与播报流互斥（打断）

  /**
   * 语音播报抢占：把戏曲压住（不是停）。记下「压之前是否在播」，
   * `resumeAfterSpeech()` 只在这种情况下回放——用户自己按的暂停不会被顶开。
   */
  duckForSpeech(): void {
    if (this.disposed) return;
    if (this.status !== "playing") return;
    this.ducked = true;
    this.resumeAfterDuck = true;
    this.audio?.pause();
    this.setStatus("paused");
  }

  /** 播报结束：若这一轮是「被压住的」且用户没主动停，就继续放。 */
  resumeAfterSpeech(): void {
    if (this.disposed) return;
    if (!this.ducked || !this.resumeAfterDuck) return;
    this.ducked = false;
    this.resumeAfterDuck = false;
    void this.apply("resume");
  }

  /** 会话拆线/离开页面时调用：停播并释放元素，不再回调状态。 */
  dispose(): void {
    this.disposed = true;
    if (this.audio) {
      this.audio.pause();
      this.audio.src = "";
    }
    this.audio = null;
    this.track = null;
    this.status = "idle";
  }
}
