import { useEffect, useRef, useState } from "react";
import type { PadStatus } from "./types";
import type { SubtitleSource } from "./components/BigSubtitle";
import type { DeviceSession } from "./session";
import { sessionValid, loadSession, clearSession } from "./session";
import { StatusBar } from "./components/StatusBar";
import { BigSubtitle } from "./components/BigSubtitle";
import { LoginPage } from "./pages/LoginPage";
import { RealtimeClient } from "./realtime";
import "./index.css";

interface SubtitleState {
  text: string;
  source: SubtitleSource;
}

/** 主界面：接真链路（WS 信令 + 麦克风 + VAD + WebRTC），步骤 8 起 */
function MainScreen({ onLogout }: { onLogout: () => void }) {
  const [status, setStatus] = useState<PadStatus>("connecting");
  const [subtitle, setSubtitle] = useState<SubtitleState>({
    text: "",
    source: "final",
  });
  const audioRef = useRef<HTMLAudioElement>(null);

  useEffect(() => {
    const session = loadSession();
    if (!session || !audioRef.current) return;
    const client = new RealtimeClient({
      onStatus: setStatus,
      onSubtitle: (text, source) => setSubtitle({ text, source }),
      onAuthFail: onLogout, // 会话过期/无效 → 回登录页（T-H 兜底口径）
      audioEl: audioRef.current,
    });
    void client.start(session.access_token);
    return () => {
      void client.stop();
    };
    // onLogout 由调用方保证稳定；此处仅随挂载启停
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex h-full flex-col">
      {/* TTS 下行播放（WebRTC 收轨） */}
      <audio ref={audioRef} autoPlay />

      {/* 顶栏：状态条（1920×1080 布局，左侧安全边距）+ 退出（自检用） */}
      <header className="flex items-center justify-between px-12 pt-8">
        <StatusBar status={status} />
        <button
          onClick={onLogout}
          className="rounded-xl bg-slate-800 px-4 py-2 text-xl text-slate-400 active:bg-slate-700"
        >
          退出登录
        </button>
      </header>

      {/* 中部：3D 数字人区（Day 4 接 three-vrm；加载失败/超 5s 切静态头像，不白屏）。
          min-h-0 + min() 尺寸：1920×1080 为基线，小视口下先收缩本区，保证大字幕不被截断 */}
      <main className="flex min-h-0 flex-1 items-center justify-center">
        <div
          className="flex items-center justify-center rounded-full bg-slate-800/60"
          style={{ height: "min(560px, 48vh)", width: "min(560px, 48vh)" }}
        >
          <span className="text-4xl text-slate-500">数字人占位</span>
        </div>
      </main>

      {/* 底部：大字幕区（partial/final/reply/notice 同位切换） */}
      <footer className="pb-16">
        <BigSubtitle text={subtitle.text} source={subtitle.source} />
      </footer>
    </div>
  );
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
  return <MainScreen onLogout={handleLogout} />;
}
