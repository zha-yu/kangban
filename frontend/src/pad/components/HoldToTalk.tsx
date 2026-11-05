/**
 * T-G 按住说话组件：WS/WebRTC 主链路不可用时的降级入口（C3 REST 整段上传）。
 * 按下开始攒帧（preRoll + 按住期间帧），松开组 WAV 上传；结果经回调渲染，全程不白屏。
 * 自持独立 MicCapture（主链路断开时其实例已停，无设备竞争；占用错误走既有话术映射）。
 */
import { useRef, useState } from "react";
import { MicCapture } from "../audio/capture";
import { sendUtterance } from "../rest-client";
import type { SubtitleSource } from "./BigSubtitle";

interface Props {
  token: string;
  onSubtitle: (text: string, source: SubtitleSource) => void;
  onAuthFail: () => void;
}

export function HoldToTalk({ token, onSubtitle, onAuthFail }: Props) {
  const [holding, setHolding] = useState(false);
  const [busy, setBusy] = useState(false);
  const micRef = useRef<MicCapture | null>(null);
  const framesRef = useRef<Int16Array[]>([]);
  const startedAtRef = useRef(0);
  /** begin/end 竞态守卫：每次 begin 递增；start 返回后发现已被新按下/松开作废则不接管采集 */
  const seqRef = useRef(0);

  async function begin(): Promise<void> {
    const seq = ++seqRef.current;
    framesRef.current = [];
    startedAtRef.current = Date.now();
    setHolding(true);
    onSubtitle("在听您说，说完请松开", "notice");
    if (!micRef.current) micRef.current = new MicCapture();
    const mic = micRef.current;
    if (!mic.stream) {
      try {
        await mic.start();
      } catch (err) {
        onSubtitle(err instanceof Error ? err.message : "麦克风启动失败", "notice");
        setHolding(false);
        return;
      }
    }
    if (seqRef.current !== seq) {
      // 已被 end() 作废（快速点按）：释放本次启动的采集，避免悬挂
      await mic.stop();
      return;
    }
    mic.onFrame = (pcm) => framesRef.current.push(pcm);
  }

  async function end(): Promise<void> {
    seqRef.current++; // 作废任何在途的 begin()
    setHolding(false);
    const mic = micRef.current;
    if (!mic) return;
    mic.onFrame = null;
    // preRoll 补句首（600ms 环形缓冲），随后接按住期间整段
    const frames = [...mic.takePreRoll(), ...framesRef.current];
    const durationMs = Date.now() - startedAtRef.current;
    framesRef.current = [];
    if (frames.length === 0) {
      // 快速点击/无音频：明确提示而不是停在「在听您说」
      onSubtitle("没听清，请按住按钮再说话", "notice");
      return;
    }

    setBusy(true);
    onSubtitle("正在想…", "notice");
    const result = await sendUtterance(token, frames, durationMs);
    setBusy(false);

    if ("kind" in result) {
      if (result.kind === "auth") {
        onAuthFail();
        return;
      }
      onSubtitle(result.message ?? "语音服务暂时不可用，请稍后再试", "notice");
      return;
    }
    // 成功：识别文本 + 回复话术 + 可选服务端音频
    onSubtitle(result.text, "final");
    if (result.replyText) onSubtitle(result.replyText, "reply");
    if (result.replyAudioUrl) {
      const player = new Audio(new URL(result.replyAudioUrl, location.href).href);
      void player.play().catch(() => {
        onSubtitle("点击屏幕后开始播放语音", "notice");
      });
    }
  }

  return (
    <button
      disabled={busy}
      onPointerDown={() => void begin()}
      onPointerUp={() => void end()}
      onPointerLeave={() => holding && void end()}
      className={
        holding
          ? "rounded-2xl bg-brand-strong px-16 py-8 text-5xl font-bold text-ink-inverse"
          : "rounded-2xl bg-brand px-16 py-8 text-5xl font-bold text-ink-inverse active:bg-brand-strong disabled:opacity-50"
      }
    >
      {busy ? "思考中…" : holding ? "松开 结束" : "按住 说话"}
    </button>
  );
}
