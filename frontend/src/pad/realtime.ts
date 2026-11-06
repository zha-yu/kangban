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
 */
import type { PadStatus } from "./types";
import type { SubtitleSource } from "./components/BigSubtitle";
import type { ClientDiagStage, ServerSignal } from "./protocol";
import { MicCapture } from "./audio/capture";
import { TenVad } from "./audio/vad";
import { SignalSocket } from "./signal-socket";

export interface RealtimeCallbacks {
  onStatus: (status: PadStatus) => void;
  onSubtitle: (text: string, source: SubtitleSource) => void;
  onAuthFail: () => void;
  /** 连续 5 次自动重连失败：上层出「重新连接」按钮，用户点击后调 retry() */
  onNeedManualRetry: () => void;
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
  /** 信令就绪 = auth_ok 已到（SocketState "ready"）：client_diag 只在此时放行（C2：auth 前上行会被 4401 关连） */
  private socketReady = false;
  /** TTS 是否在播（打断判定用；tts_text 置位，打断/新一轮 partial 复位） */
  private ttsPlaying = false;

  constructor(private cb: RealtimeCallbacks) {}

  async start(token: string): Promise<void> {
    if (this.starting) return;
    this.starting = true;

    this.socket = new SignalSocket(token);
    this.socket.onStateChange = (state) => {
      this.socketReady = state === "ready";
      if (state === "connecting" || state === "closed") this.cb.onStatus("disconnected");
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

  /** C5 客户端诊断上行（仅日志，服务端不回执）：未就绪时静默丢弃，不排队不重发 */
  sendDiag(stage: ClientDiagStage, ok: boolean, error: string): void {
    if (!this.socketReady) return;
    this.socket?.send({ type: "client_diag", stage, ok, error });
  }

  async stop(): Promise<void> {
    this.starting = false;
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
        this.ttsPlaying = false; // 新一轮识别，打断态复位
        this.cb.onSubtitle(msg.text, "partial");
        this.cb.onStatus("listening");
        break;
      case "asr_final":
        this.cb.onSubtitle(msg.text, "final");
        this.cb.onStatus("thinking");
        break;
      case "tts_text":
        this.ttsPlaying = true;
        this.cb.audioEl.muted = false; // 真 TTS 到了才出声
        void this.cb.audioEl.play().catch(() => {});
        this.cb.onSubtitle(msg.text, "reply");
        this.cb.onStatus("speaking");
        break;
      case "error":
        this.cb.onSubtitle(msg.message, "notice");
        break;
      case "pong":
      case "auth_fail":
        break; // auth_fail 已由 SignalSocket 处理
    }
  }

  /** auth_ok 后建立一轮会话：麦克风 + VAD + PeerConnection + offer */
  private async setupSession(): Promise<void> {
    // 1. 麦克风
    this.mic = new MicCapture();
    try {
      await this.mic.start();
    } catch (err) {
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
          }
          this.cb.onStatus("listening");
          this.socket?.send({ type: "asr_start" });
          this.mic?.flushPreRollToTrack(); // asr_start 后立即轨上回放 600ms preRoll（句首保完整）
        },
        onSpeechEnd: () => {
          this.cb.onStatus("thinking");
          this.socket?.send({ type: "asr_end" });
        },
      });
    } catch (err) {
      console.warn("[vad] 端侧 TEN-VAD 不可用，回落服务端 webrtcvad:", err);
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

  private async teardownSession(): Promise<void> {
    this.vad?.free();
    this.vad = null;
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
    if (!this.mic || !this.pc) return;
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
