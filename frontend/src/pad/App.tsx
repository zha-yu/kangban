import { useCallback, useEffect, useRef, useState } from "react";
import type { PadStatus } from "./types";
import type { SubtitleSource } from "./components/BigSubtitle";
import type { DeviceSession } from "./session";
import { sessionValid, loadSession, clearSession } from "./session";
import { StatusBar } from "./components/StatusBar";
import { BigSubtitle } from "./components/BigSubtitle";
import { StatusBadge } from "./components/StatusBadge";
import { HoldToTalk } from "./components/HoldToTalk";
import { LoginPage } from "./pages/LoginPage";
import { RealtimeClient } from "./realtime";
import { MicCapture } from "./audio/capture";
import { loadKwsDetector, type KwsDetector } from "./kws/kws-detector";
import { AvatarStage } from "./avatar/AvatarStage";
import { getVoiceprintStatus } from "./voiceprint/voiceprint-client";
import { VoiceprintEnroll } from "./voiceprint/VoiceprintEnroll";
import "./index.css";

interface SubtitleState {
  text: string;
  source: SubtitleSource;
}

/** 主界面：两相轮转（Day 4 W2）——
 *  standby：待机唤醒期，KWS 端侧监听「小伴」（音频不出端）+ 呼叫按钮正交入口（FR-UI-09）；
 *  inSession：命中或点按钮后起 RealtimeClient 走既有链路；断连/连续重连失败自动回 standby。
 *  initialNotice：声纹状态查询失败但放行进对话时，大字幕先出的一行系统提示（C4 口径：查询失败不把长者挡在门外）。 */
function MainScreen({
  onLogout,
  initialNotice,
}: {
  onLogout: () => void;
  initialNotice?: string;
}) {
  const [phase, setPhase] = useState<"standby" | "inSession">("standby");
  const [status, setStatus] = useState<PadStatus>("disconnected");
  const [subtitle, setSubtitle] = useState<SubtitleState>({
    text: initialNotice ?? "",
    source: initialNotice ? "notice" : "final",
  });
  // R-5 可用性探测三态：loading（探测中）→ listening（唤醒词可用）/ unavailable（只留呼叫按钮）
  const [kwsState, setKwsState] = useState<"loading" | "listening" | "unavailable">("loading");
  const audioRef = useRef<HTMLAudioElement>(null);
  const clientRef = useRef<RealtimeClient | null>(null);
  const kwsRef = useRef<{ detector: KwsDetector; mic: MicCapture } | null>(null);

  /** 进入会话：停 KWS（关麦 + 释放回调，麦克风交给 RealtimeClient 自建实例），切换相位 */
  const beginSession = useCallback(() => {
    const k = kwsRef.current;
    kwsRef.current = null;
    if (k) {
      k.detector.close();
      void k.mic.stop();
      setKwsState("loading"); // 会话结束后回 standby 会重新探测
    }
    setPhase("inSession");
  }, []);

  /** 回到待机：停既有链路（effect 清理里 stop），重挂 KWS 监听 */
  const endSession = useCallback(() => {
    setPhase("standby");
    setStatus("disconnected");
  }, []);

  // standby 期：KWS 可用性探测 + 端侧监听（R-5：探测失败落呼叫按钮降级，不阻塞）
  useEffect(() => {
    if (phase !== "standby") return;
    let cancelled = false;
    void (async () => {
      const detector = await loadKwsDetector();
      if (cancelled) return;
      if (!detector) {
        setKwsState("unavailable");
        return;
      }
      const mic = new MicCapture(); // 独立实例：唤醒期与 RealtimeClient 的实例互不相干
      try {
        await mic.start();
      } catch {
        setKwsState("unavailable"); // 麦克风拿不到 → 按钮降级
        return;
      }
      if (cancelled) {
        void mic.stop(); // 双跑竞态守卫：cleanup 已跑则立即释放，防双麦
        return;
      }
      detector.onHit = beginSession;
      mic.onFrame = (pcm) => detector.feed(pcm); // 仅本机推理，不出端（AGENTS 红线 10）
      kwsRef.current = { detector, mic };
      setKwsState("listening");
    })();
    return () => {
      cancelled = true;
    };
  }, [phase, beginSession]);

  // inSession 期：起 RealtimeClient（原主链路逻辑）
  useEffect(() => {
    if (phase !== "inSession") return;
    const session = loadSession();
    if (!session || !audioRef.current) {
      setPhase("standby");
      return;
    }
    const client = new RealtimeClient({
      onStatus: (s) => {
        setStatus(s);
        if (s === "disconnected") endSession(); // 会话结束/断连 → 回待机（呼叫按钮与唤醒词重新可用）
      },
      onSubtitle: (text, source) => setSubtitle({ text, source }),
      onAuthFail: onLogout, // 会话过期/无效 → 回登录页（T-H 方案 A 定案）
      onNeedManualRetry: endSession, // 连续 5 次重连失败 → 同样回待机，按钮重新出现
      audioEl: audioRef.current,
    });
    clientRef.current = client;
    void client.start(session.access_token);

    // T-H 方案 A 定案（无 refresh 通道，过期回登录）：会话中途到期的主动守护——
    // 30s 周期 + 页面回前台时校验 expires_at，过期即清会话回登录页；
    // WS 4401/auth_fail 与 REST 401 由各自链路回调 onLogout 兜住
    const expiryGuard = setInterval(() => {
      if (!sessionValid()) onLogout();
    }, 30_000);
    const onVisible = () => {
      if (document.visibilityState === "visible" && !sessionValid()) onLogout();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(expiryGuard);
      document.removeEventListener("visibilitychange", onVisible);
      void client.stop();
      clientRef.current = null;
    };
    // onLogout/endSession 由调用方保证稳定；此处仅随相位启停
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  // 卸载兜底：停掉待机期的 KWS 麦克风
  useEffect(
    () => () => {
      const k = kwsRef.current;
      if (k) {
        k.detector.close();
        void k.mic.stop();
      }
    },
    [],
  );

  const standbyHint =
    kwsState === "listening"
      ? "说“小伴”开始对话"
      : kwsState === "loading"
        ? "正在准备唤醒词…"
        : "点下方按钮开始对话"; // R-5：KWS 不可用只留按钮，不误导长者喊唤醒词

  return (
    <div className="flex h-full flex-col">
      {/* TTS 下行播放（WebRTC 收轨）；传给 AvatarStage 做口型（P1） */}
      <audio ref={audioRef} />

      {/* 顶栏：状态条（1920×1080 布局，左侧安全边距）+ 退出（自检用） */}
      <header className="flex items-center justify-between px-12 pt-8">
        <StatusBar status={status} />
        <button
          onClick={onLogout}
          className="rounded-2xl border border-line bg-surface px-5 py-4 text-xl text-ink-muted active:bg-surface-sunken"
        >
          退出登录
        </button>
      </header>

      {/* 中部：3D 数字人舞台（Day 4 W1：C7 avatar-config + three-vrm；加载失败/超 5s 切静态头像，不白屏）。
          圆框尺寸沿用 min(560px,48vh)：1920×1080 为基线，小视口下先收缩本区，保证大字幕不被截断 */}
      <main className="flex min-h-0 flex-1 items-center justify-center">
        <AvatarStage
          token={loadSession()?.access_token ?? ""}
          onAuthFail={onLogout}
          sendDiag={(stage, ok, error) => clientRef.current?.sendDiag(stage, ok, error)}
          audioEl={phase === "inSession" ? audioRef.current : null} // 仅会话期有 TTS 下行
        />
      </main>

      {/* 底部：standby 期为唤醒引导 + 呼叫按钮；inSession 为大字幕区（partial/final/reply/notice 同位切换） */}
      <footer className="pb-16">
        {phase === "standby" ? (
          <div className="flex flex-col items-center gap-8">
            <BigSubtitle text={standbyHint} source="notice" />
            <button
              onClick={beginSession}
              className="rounded-2xl bg-brand px-16 py-6 text-4xl font-bold text-ink-inverse active:bg-brand-strong"
            >
              呼叫小伴
            </button>
            {/* T-G 降级入口：主链路未建立/已断开时按住说话走 C3 REST 整段上传 */}
            <HoldToTalk
              token={loadSession()?.access_token ?? ""}
              onSubtitle={(text, source) => setSubtitle({ text, source })}
              onAuthFail={onLogout}
            />
          </div>
        ) : (
          <BigSubtitle text={subtitle.text} source={subtitle.source} />
        )}
      </footer>
    </div>
  );
}

/**
 * 声纹门卫（C4 语义）：登录后、RealtimeClient 启动前先查 GET /api/voiceprints/status，
 * 三分支：409 device_unbound → 大字提示不进对话（未绑定长者不该起对话链路）；
 * none/partial → VoiceprintEnroll 录入引导，onDone 后进对话（3 段齐即 active，无需复查）；
 * active → 直接进对话。查询失败 / 网络异常不阻塞放行，大字幕出一行提示。
 */
type VoiceprintPhase = "checking" | "unbound" | "enroll" | "ready";

function VoiceprintGate({ onLogout }: { onLogout: () => void }) {
  const [phase, setPhase] = useState<VoiceprintPhase>("checking");
  const [notice, setNotice] = useState<string | null>(null);

  const check = useCallback(async () => {
    const session = loadSession();
    if (!session) {
      onLogout();
      return;
    }
    const result = await getVoiceprintStatus(session.access_token);
    if ("kind" in result) {
      if (result.kind === "auth") {
        onLogout(); // 会话失效 → 回登录
        return;
      }
      if (result.kind === "device_unbound") {
        setPhase("unbound");
        return;
      }
      // unavailable（503/网络/超时）：不把长者挡在门外，直接进对话 + 大字幕一行提示
      setNotice("声纹状态暂时没查到，先陪您聊天；需要录入时请稍后再试");
      setPhase("ready");
      return;
    }
    setNotice(null);
    setPhase(result.status === "active" ? "ready" : "enroll");
  }, [onLogout]);

  useEffect(() => {
    void check();
  }, [check]);

  if (phase === "checking") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-6">
        <p className="text-5xl font-bold text-ink">正在准备…</p>
        <p className="text-2xl text-ink-muted">正在确认您的声纹状态</p>
      </div>
    );
  }

  if (phase === "unbound") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-10 px-24 text-center">
        <StatusBadge tone="warning" size="lg">
          设备未绑定
        </StatusBadge>
        <p className="text-5xl font-bold leading-snug text-ink">
          设备还没有绑定长者，请在照护台完成绑定
        </p>
        <p className="text-2xl text-ink-muted">绑定完成后，点下面的按钮继续</p>
        <button
          type="button"
          onClick={() => void check()}
          className="h-24 rounded-2xl bg-brand px-16 text-3xl font-bold text-ink-inverse active:bg-brand-strong"
        >
          我已完成绑定，重新检查
        </button>
      </div>
    );
  }

  if (phase === "enroll") {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <VoiceprintEnroll
          token={loadSession()?.access_token ?? ""}
          onAuthFail={onLogout}
          onDone={() => setPhase("ready")}
        />
      </div>
    );
  }

  return <MainScreen onLogout={onLogout} initialNotice={notice ?? undefined} />;
}

export function App() {
  // 会话本体存 sessionStorage（session.ts）；组件只持有登录态
  const [loggedIn, setLoggedIn] = useState(() => sessionValid());

  function handleLoggedIn(_s: DeviceSession) {
    setLoggedIn(true);
  }

  function handleLogout() {
    clearSession();
    setLoggedIn(false);
  }

  if (!loggedIn) return <LoginPage onLoggedIn={handleLoggedIn} />;
  return <VoiceprintGate onLogout={handleLogout} />;
}
