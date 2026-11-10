/**
 * T-E 实时链路编排器：信令 WS + 麦克风采集 + 端侧 TEN-VAD + WebRTC 收发。
 * 主链路口径（adhoc §3/§4 + A2 裁决 2026-11-05）：麦克风 16k 帧喂端侧 VAD；
 * speechstart → 发 asr_start → worklet 立即把 preRoll 环形缓冲经轨回放补发
 * （服务端不回溯轨道，asr_start 后服务端才开始消费识别流，句首 600ms 靠补发）；
 * speechend → 发 asr_end；WebRTC 音频轨连续上行。主线程 preRoll 环形缓冲
 * 仅用于 C3 降级 WAV，与轨上回放相互独立。
 * 打断（A2 裁决）：前端本地 speechstart 同一刻立即停播，不等服务端；
 * 服务端 A3 收到 asr_start 停写轨清缓冲（A3 媒体层未落码前联调不会中断）。
 * TEN-VAD 加载失败 → vadFallback：不发 asr_start/end，由服务端 webrtcvad 判定（字幕连续）。
 * Day 6（N16）：tts_text 带分段字段（contracts.md v1.0 §4）→ PlaybackQueue 结算
 * playing/played/canceled（「下一段到达即结算」口径，见 playback-queue.ts 头注释）；
 * 无分段字段 → 整段一播原路径不动（回滚兼容）。C-6 计时改一段一次：上一段未结算
 * 则在新段到达时先结算。
 * Day 7 D1（S1.2，docs/plans/day07/d-推进计划-pad可靠性改绑联调.md）：
 *  tts_text 先过队列淘汰判定再放开播放态（P2-22）；teardown/setup 解绑
 *  devicechange 防重连累积 listener（P2-23）；reopenMic 并发守卫、mic.start 失败
 *  上报诊断、stop/teardown 复位播放态、tts_text play() 失败给提示（P3-38）；
 *  speechstart 本地停播后立即上行 interrupt（P1-3 前端半边，字段按 Day7 README
 *  §5.4 草案，待 A1 契约冻结校对）；路由降级提示预留（A3 下发 degraded 标记时显示）。 */
import type { PadStatus } from "./types";
import type { SubtitleSource } from "./components/BigSubtitle";
import type {
  ClientDiagMsg,
  ClientDiagStage,
  IdentityAskMsg,
  IdentityResultMsg,
  InterruptMsg,
  ServerSignal,
  TaskCommandMsg,
  TtsTextMsg,
} from "./protocol";
import { nextDegradeLevel, type DegradeLevel } from "./degrade";
import { MicCapture } from "./audio/capture";
import { TenVad } from "./audio/vad";
import { PlaybackQueue, isSegmentedTts } from "./audio/playback-queue";
import { TouchPlayback } from "./audio/touch-audio";
import { SignalSocket } from "./signal-socket";

/** Day 7 D1 S1.2.7 预留：路由降级常量文案（A3 下发降级标记时显示；不新增协议字段） */
const FALLBACK_NOTICE = "网络慢，正在换种方式回答";

export interface RealtimeCallbacks {
  onStatus: (status: PadStatus) => void;
  onSubtitle: (text: string, source: SubtitleSource) => void;
  onAuthFail: () => void;
  /** 连续 5 次自动重连失败：上层出「重新连接」按钮，用户点击后调 retry() */
  onNeedManualRetry: () => void;
  /** C5：声纹低置信/不可用 → 上层弹大按钮确认面板（30s 内不回 confirm 服务端落 generic） */
  onIdentityAsk: (msg: IdentityAskMsg) => void;
  /** C5：身份定案（voice/confirm/generic）；generic 时 elder_id 为 null */
  onIdentityResult: (msg: IdentityResultMsg) => void;
  /** Day 7 L6#9：主动触达任务卡片命令 → 上层 emitPadTask 打开面板 */
  onTaskCommand: (msg: TaskCommandMsg) => void;
  /** TTS 下行播放元素（登录点击后已有用户手势，autoplay 可用） */
  audioEl: HTMLAudioElement;
}

export class RealtimeClient {
  private socket: SignalSocket | null = null;
  private mic: MicCapture | null = null;
  private vad: TenVad | null = null;
  private pc: RTCPeerConnection | null = null;
  private unbindDeviceChange: (() => void) | null = null;
  private starting = false;
  /** Day 7 D1 关 P3-38：reopenMic 并发守卫（重连进行中直接返回，防级联重入） */
  private reopenMicBusy = false;
  /** 信令就绪 = auth_ok 已到（SocketState "ready"）：client_diag 只在此时放行（C2：auth 前上行会被 4401 关连） */
  private socketReady = false;
  /** TTS 是否在播（打断判定用；tts_text 置位，打断/新一轮 partial 复位） */
  private ttsPlaying = false;
  /** C-1 本轮降级提示级别（degrade.ts 状态机）：speechstart/asr_partial 新一轮复位 */
  private degradeLevel: DegradeLevel | null = null;
  /** C-6 playback 段计时起点（tts_text 到达即起表；播完/打断结算上报后清空） */
  private playbackStartMs: number | null = null;
  /** Day 6 N16：分段播放队列与送达确认（仅消费带分段字段的 tts_text） */
  private playbackQueue = new PlaybackQueue({
    sendAck: (msg) => {
      if (!this.socketReady || this.socket?.ready !== true) return false;
      this.socket.send(msg);
      return true;
    },
    sendDiag: (ok, error) => this.sendDiag("playback", ok, error),
    // Day 6：末段兜底结算收口回调——轨 ended 不触发时由队列计时器结算 played，
    // 此处同步复位播放态与状态条（与 onended 路径行为一致，幂等）
    onResponseSettled: () => {
      if (this.ttsPlaying) {
        this.ttsPlaying = false;
        this.reportPlayback(); // C-6：末段播完结算（无计时起点时内部静默跳过）
        this.cb.onStatus("listening"); // 播放收口 → 回到在听，麦克风保持捕获
      }
    },
  });
  /** Day 7 TD-1/TD-2：触达音频独立播放轨（WS base64 PCM，不进 WebRTC 轨、不进队列）。
   *  在途触达的回传坐标（touch_id → 服务端下发的 event_id/attempt_no，播完原样回传） */
  private touchMeta = new Map<string, { event_id: string; attempt_no: number }>();
  private touchPlayback = new TouchPlayback({
    onPlayed: (touchId) => this.sendTouchPlayed(touchId),
    sendDiag: (ok, error) => this.sendDiag("playback", ok, error),
  });

  constructor(private cb: RealtimeCallbacks) {}

  async start(token: string, wakeTs?: number): Promise<void> {
    if (this.starting) return;
    this.starting = true;

    this.socket = new SignalSocket(token);
    if (wakeTs !== undefined) this.socket.setWakeTs(wakeTs); // C-5：KWS 命中时刻随 auth 首帧
    this.socket.onStateChange = (state) => {
      this.socketReady = state === "ready";
      // connecting ≠ 断开：状态条显示「连接中」；否则上层会把建连初期误判为会话结束
      if (state === "connecting") this.cb.onStatus("connecting");
      if (state === "closed") this.cb.onStatus("disconnected");
    };
    this.socket.onAuthFail = this.cb.onAuthFail;
    this.socket.onGiveUp = this.cb.onNeedManualRetry;
    this.socket.onServerSignal = (msg) => void this.handleSignal(msg);
    this.socket.connect();
  }

  /** 「重新连接」按钮：复位信令状态机并重连（会话组件不重建） */
  retry(): void {
    this.socket?.retry();
  }

  /** C5 客户端诊断上行（仅日志，服务端不回执）：未就绪时静默丢弃，不排队不重发。
   *  durationMs 仅 stage=playback 携带（C2/C5 契约 v1.5，服务端代打 metrics） */
  sendDiag(stage: ClientDiagStage, ok: boolean, error: string, durationMs?: number): void {
    if (!this.socketReady) return;
    const msg: ClientDiagMsg =
      durationMs === undefined
        ? { type: "client_diag", stage, ok, error }
        : { type: "client_diag", stage, ok, error, duration_ms: durationMs };
    this.socket?.send(msg);
  }

  /** Day 7 L6#8：文本上行（任务卡片确认按钮/输入框降级）。
   *  服务端按 ASR FINAL 同路编排；会话未就绪时静默丢弃（按钮是尽力而为上行）。 */
  sendUserText(text: string): void {
    if (!this.socketReady) return;
    this.socket?.send({ type: "user_text", text });
  }

  async stop(): Promise<void> {
    this.starting = false;
    this.ttsPlaying = false; // Day 7 D1 关 P3-38：登出复位播放态，防下次会话误判打断
    this.unbindDeviceChange?.();
    this.unbindDeviceChange = null;
    this.socket?.stop();
    this.socket = null;
    await this.teardownSession();
  }

  private async handleSignal(msg: ServerSignal): Promise<void> {
    switch (msg.type) {
      case "auth_ok":
        await this.teardownSession(); // 重连场景先清旧会话
        await this.setupSession();
        break;
      case "answer":
        if (this.pc?.signalingState !== "stable") {
          await this.pc?.setRemoteDescription({ type: "answer", sdp: msg.sdp });
        }
        break;
      case "ice":
        try {
          await this.pc?.addIceCandidate(JSON.parse(msg.candidate));
        } catch {
          /* 迟到/无效候选丢弃，不中断 */
        }
        break;
      case "asr_partial":
        // 服务端 VAD 兜底路径本地无 speechstart：识别出新的一轮 → 补一次打断收口
        //（正常路径已在 speechstart 收口过，此处为幂等 no-op）
        if (this.ttsPlaying) this.playbackQueue.interrupt();
        this.ttsPlaying = false; // 新一轮识别，打断态复位
        this.degradeLevel = null; // 新一轮对话，降级提示级别复位
        this.cb.onSubtitle(msg.text, "partial");
        this.cb.onStatus("listening");
        break;
      case "asr_final":
        this.cb.onSubtitle(msg.text, "final");
        this.cb.onStatus("thinking");
        break;
      case "tts_text":
        // Day 7 TD-1：触达消息（带 touch_id）走独立音频通道——字幕照常渲染，
        // 但**不动** WebRTC 音频轨、不进 PlaybackQueue、不参与打断/计时逻辑
        //（与回复流互不打断、互不串流，契约 §1）。降级（无 audio 字段）= 纯字幕，不回执。
        if (msg.touch_id !== undefined) {
          this.cb.onSubtitle(msg.text, "reply");
          this.cb.onStatus("speaking");
          this.handleTouchAudio(msg);
          break;
        }
        // Day 7 D1 关 P2-22：分段消息先过队列淘汰判定——旧 epoch 段不播、不 ack、
        // 不改 ttsPlaying/状态条、不渲染字幕（防止覆盖新一代回复的字幕）
        if (isSegmentedTts(msg) && this.playbackQueue.isStale(msg)) break;
        // C-6 一段一次计时：分段模式下段到达即上一段播完，先结算上一段再起表；
        // 整段模式两次 tts_text 间无此场景（服务端一轮只发一条），结算为无害兜底
        this.reportPlayback();
        this.playbackStartMs = performance.now();
        this.ttsPlaying = true;
        this.cb.audioEl.muted = false; // 真 TTS 到了才出声
        void this.cb.audioEl
          .play()
          // Day 7 D1 关 P3-39：实际起播成功后补发 pending 的 playing ack
          .then(() => this.playbackQueue.notifyPlaybackStarted())
          // Day 7 D1 关 P3-38：失败不再静默（与 ontrack 路径话术一致）
          .catch(() => {
            this.cb.onSubtitle("点击屏幕后开始播放语音", "notice");
          });
        this.cb.onSubtitle(msg.text, "reply");
        this.cb.onStatus("speaking");
        // Day 7 D1 S1.2.7 预留：A3 下发降级标记时显示常量文案（不新增协议字段，
        // A3 未下发则永不触发，不阻塞 D1）
        if (msg.degraded === true) {
          this.cb.onSubtitle(FALLBACK_NOTICE, "notice");
        }
        // Day 6：带分段字段 → 队列结算 playing/played（playing 由起播后补发）
        if (isSegmentedTts(msg)) this.playbackQueue.handleSegment(msg);
        break;
      case "error": {
        // C-1（C-D4 方案 A）：slow 为占位级，unavailable/timeout 为失败终态；
        // 失败已显后同轮迟到的 slow 压制不渲染，防止话术从失败回退成等待
        const level = nextDegradeLevel(this.degradeLevel, msg.code);
        if (level === "suppressed") break;
        this.degradeLevel = level;
        this.cb.onSubtitle(msg.message, "notice");
        // 失败终态（unavailable/timeout/internal/tool_breaker）：本轮已结束，
        // 状态从「正在想/正在说」拉回「在听」，避免服务端异常时状态条永久卡住
        if (level === "failure") this.cb.onStatus("listening");
        break;
      }
      case "pong":
      case "auth_fail":
        break; // auth_fail 已由 SignalSocket 处理
      case "identity_ask":
        this.cb.onIdentityAsk(msg);
        break;
      case "identity_result":
        // C5 定案即本轮结束：触发确认的那句话不会被回复（已被身份流程消费），
        // 状态从「正在想」拉回「在听」，由上层提示长者再讲一遍
        this.cb.onStatus("listening");
        this.cb.onIdentityResult(msg);
        break;
      case "task_command":
        // Day 7 L6#9：主动触达卡片命令 → 上层任务总线（幂等由 App 按 taskId 处理）
        this.cb.onTaskCommand(msg);
        break;
    }
  }

  /** C5：长者点选确认（elder_id 必须回带 candidates[0].elder_id；「不是我」不回，等 30s 落 generic） */
  confirmIdentity(askId: string, elderId: string): void {
    this.socket?.send({ type: "identity_confirm", ask_id: askId, elder_id: elderId });
  }

  /** auth_ok 后建立一轮会话：麦克风 + VAD + PeerConnection + offer */
  private async setupSession(): Promise<void> {
    // Day 7 D1 关 P2-23：建新监听前先解绑旧引用，重连不累积 devicechange listener
    this.unbindDeviceChange?.();
    this.unbindDeviceChange = null;
    // 1. 麦克风
    this.mic = new MicCapture();
    try {
      await this.mic.start();
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      // Day 7 D1 关 P3-38：mic.start 失败必须上报诊断（此前静默无 offer，
      // 服务端 VadFallbackController 无法感知）
      this.sendDiag("webrtc", false, `mic_start_failed: ${reason}`);
      this.cb.onSubtitle(err instanceof Error ? err.message : "麦克风启动失败", "notice");
      this.mic = null;
      return;
    }
    this.mic.onFrame = (pcm) => this.vad?.feed(pcm);

    // 2. 端侧 VAD（失败回落服务端 webrtcvad）
    try {
      this.vad = await TenVad.load({
        onSpeechStart: () => {
          // T-F 打断（A2 裁决定案）：TTS 在播时本地开口 → 立即停播 + 清回复字幕，
          // 不等服务端；服务端 A3 收到 asr_start 停写轨清缓冲
          if (this.ttsPlaying) {
            this.ttsPlaying = false;
            this.cb.audioEl.muted = true; // 打断即静音，输出通路立即关闭
            this.cb.audioEl.pause();
            this.cb.onSubtitle("", "final");
            this.reportPlayback(); // C-6：打断也结算（实际已播时长，如实入档）
            // Day 7 D1 关 P1-3（前端半边）：本地停播后立即上行打断，不等回执。
            // response_id 取当前活跃回复（S1.1 配套暴露），无活跃回复不发送；
            // 字段按 Day7 README §5.4 草案，待 A1 契约冻结后逐字段校对
            const responseId = this.playbackQueue.activeResponseId;
            if (responseId !== null) {
              const interrupt: InterruptMsg = {
                type: "interrupt",
                response_id: responseId,
                reason: "barge_in",
              };
              this.socket?.send(interrupt);
            }
            // Day 6：canceled 收口（segment_id 填最后已播完段，无则 -1——dev-C 口径转达件③）
            this.playbackQueue.interrupt();
          }
          this.degradeLevel = null; // 新一轮开口，降级提示级别复位
          this.cb.onStatus("listening");
          this.socket?.send({ type: "asr_start" });
          this.mic?.flushPreRollToTrack(); // asr_start 后立即轨上回放 600ms preRoll（句首保完整）
        },
        onSpeechEnd: () => {
          this.cb.onStatus("thinking");
          this.socket?.send({ type: "asr_end" });
        },
        // Day 7 D1 关 P1-6：运行期 VAD 错误/遥测上报（acceptWaveform 异常、
        // vad_no_speech_120s 心跳、运行期脚本错误），触发服务端 webrtcvad 兜底；
        // 心跳同时作为现场收音复测的观测点
        onRuntimeError: (message) => {
          this.sendDiag("vad", false, `vad_runtime_error: ${message}`);
        },
      });
    } catch (err) {
      console.warn("[vad] 端侧 TEN-VAD 不可用，回落服务端 webrtcvad:", err);
      // W4：端侧 VAD 自检失败必须上报，否则服务端 VadFallbackController 永远不激活（听不见）
      this.sendDiag("vad", false, err instanceof Error ? err.message : String(err));
      this.cb.onSubtitle("语音检测已切换为服务端模式", "notice");
    }

    // 3. WebRTC：上行麦克风轨 + offer
    const stream = this.mic.stream;
    if (!stream) return;
    const pc = new RTCPeerConnection();
    this.pc = pc;
    for (const track of stream.getAudioTracks()) pc.addTrack(track, stream);

    pc.onicecandidate = (e) => {
      if (e.candidate) {
        this.socket?.send({ type: "ice", candidate: JSON.stringify(e.candidate) });
      }
    };
    pc.ontrack = (e) => {
      // 听写阶段不出声：静默挂载，收到 tts_text 才放开（用户裁决 2026-11-05）
      this.cb.audioEl.muted = true;
      this.cb.audioEl.srcObject = e.streams[0] ?? null;
      this.cb.audioEl.onended = () => {
        this.ttsPlaying = false;
        this.reportPlayback(); // C-6：播完结算 playback 打点
        this.playbackQueue.markTrackEnded(); // Day 6：末段 played 结算（Q-2 口径）
      };
      void this.cb.audioEl.play().catch(() => {
        this.cb.onSubtitle("点击屏幕后开始播放语音", "notice");
      });
    };
    stream.getAudioTracks()[0]?.addEventListener("ended", () => {
      this.cb.onSubtitle("麦克风已断开，正在重连…", "notice");
      void this.reopenMic();
    });

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    this.socket?.send({ type: "offer", sdp: pc.localDescription!.sdp });

    // 麦克风插拔：回落默认设备重开（adhoc §2.3）
    this.unbindDeviceChange = this.watchDeviceChange();
    this.cb.onStatus("listening");
  }

  /** Day 7 TD-1：触达音频独立播放（day07/contracts.md §1/§2）。
   *  解码/起播失败 → 降级纯字幕，不回 played（宁缺勿假）；自然播完才结算回执。 */
  private handleTouchAudio(msg: TtsTextMsg): void {
    const touchId = msg.touch_id;
    if (touchId === undefined || !msg.audio) return; // 纯字幕降级：不回执
    this.touchMeta.set(touchId, {
      event_id: msg.touch_event_id ?? "",
      attempt_no: msg.touch_attempt_no ?? 0,
    });
    if (!this.touchPlayback.play(touchId, msg.audio)) this.touchMeta.delete(touchId);
  }

  /** Day 7 TD-2：touch_played 上行（仅自然播完触发）；未就绪时静默丢弃，不排队不重发 */
  private sendTouchPlayed(touchId: string): void {
    const meta = this.touchMeta.get(touchId);
    if (!meta) return;
    this.touchMeta.delete(touchId);
    if (!this.socketReady || this.socket?.ready !== true) return;
    this.socket.send({
      type: "touch_played",
      touch_id: touchId,
      event_id: meta.event_id,
      attempt_no: meta.attempt_no,
    });
  }

  /** C-6：结算 playback 段并经 client_diag 上报（duration_ms 必填，服务端代打 metrics）；
   *  无计时起点（未播过/已结算）时静默跳过 */
  private reportPlayback(): void {
    const start = this.playbackStartMs;
    this.playbackStartMs = null;
    if (start === null) return;
    this.sendDiag("playback", true, "", Math.max(0, Math.round(performance.now() - start)));
  }

  private async teardownSession(): Promise<void> {
    // Day 7 D1 关 P2-23：拆线解绑设备监听（重连/登出路径均不残留旧 handler）
    this.unbindDeviceChange?.();
    this.unbindDeviceChange = null;
    // Day 7 D1 关 P3-38：清播放元素回调并复位静音，防旧 onended 在新会话误触发
    this.cb.audioEl.onended = null;
    this.cb.audioEl.muted = true;
    this.vad?.free();
    this.vad = null;
    this.degradeLevel = null;
    this.playbackQueue.reset(); // 重连清旧会话：分段确认状态一并复位（不发 ack）
    this.touchPlayback.stop(); // 触达音频一并停（宁缺勿假，不发 played）
    this.touchMeta.clear();
    if (this.pc) {
      this.pc.onicecandidate = null;
      this.pc.ontrack = null;
      this.pc.close();
      this.pc = null;
    }
    await this.mic?.stop();
    this.mic = null;
  }

  /** 麦克风拔出/失效 → 重新 getUserMedia 并 replaceTrack，PeerConnection 不重建 */
  private async reopenMic(): Promise<void> {
    // Day 7 D1 关 P3-38：并发守卫——重连进行中（或无可重连对象）直接返回，
    // 防多次 ended/devicechange 事件级联重入
    if (this.reopenMicBusy || !this.mic || !this.pc) return;
    this.reopenMicBusy = true;
    try {
      const sender = this.pc.getSenders().find((s) => s.track?.kind === "audio");
      await this.mic.stop();
      try {
        await this.mic.start();
      } catch (err) {
        this.cb.onSubtitle(err instanceof Error ? err.message : "麦克风重连失败", "notice");
        return;
      }
      this.mic.onFrame = (pcm) => this.vad?.feed(pcm);
      const track = this.mic.stream?.getAudioTracks()[0];
      if (track) {
        track.addEventListener("ended", () => {
          this.cb.onSubtitle("麦克风已断开，正在重连…", "notice");
          void this.reopenMic();
        });
        await sender?.replaceTrack(track);
      }
      this.cb.onStatus("listening");
    } finally {
      this.reopenMicBusy = false;
    }
  }

  private watchDeviceChange(): () => void {
    const handler = () => {
      const track = this.mic?.stream?.getAudioTracks()[0];
      if (track && track.readyState === "ended") void this.reopenMic();
    };
    navigator.mediaDevices.addEventListener("devicechange", handler);
    return () =>
      navigator.mediaDevices.removeEventListener("devicechange", handler);
  }
}
