import { useState } from "react";
import type { PadStatus } from "./types";
import type { SubtitleSource } from "./components/BigSubtitle";
import { StatusBar } from "./components/StatusBar";
import { BigSubtitle } from "./components/BigSubtitle";
import "./index.css";

interface SubtitleState {
  text: string;
  source: SubtitleSource;
}

/** 步骤 3（T-A）骨架壳；自检控制面板在步骤 8/9 接真信令后移除 */
export function App() {
  const [status, setStatus] = useState<PadStatus>("connecting");
  const [subtitle, setSubtitle] = useState<SubtitleState>({
    text: "",
    source: "final",
  });

  const statuses: PadStatus[] = [
    "listening",
    "thinking",
    "speaking",
    "connecting",
    "disconnected",
  ];
  const samples: SubtitleState[] = [
    { text: "（ASR 中间结果）今天天气…", source: "partial" },
    { text: "今天天气怎么样", source: "final" },
    { text: "今天晴，22 到 28 度，适合出门走走。", source: "reply" },
    { text: "网络不太稳定，正在重连…", source: "notice" },
  ];

  return (
    <div className="flex h-full flex-col">
      {/* 顶栏：状态条（1920×1080 布局，左侧安全边距） */}
      <header className="flex items-center px-12 pt-8">
        <StatusBar status={status} />
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

      {/* 自检控制面板（临时）：步骤 8 接真信令后移除 */}
      <section className="fixed right-8 top-8 flex flex-col gap-2 rounded-2xl bg-slate-900/80 p-4">
        <p className="text-lg text-slate-400">自检控制（临时）</p>
        <div className="flex flex-wrap gap-2">
          {statuses.map((s) => (
            <button
              key={s}
              onClick={() => setStatus(s)}
              className="rounded-xl bg-slate-700 px-4 py-2 text-xl active:bg-slate-600"
            >
              {s}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap gap-2">
          {samples.map((s, i) => (
            <button
              key={i}
              onClick={() => setSubtitle(s)}
              className="rounded-xl bg-slate-700 px-4 py-2 text-xl active:bg-slate-600"
            >
              字幕{s.source}
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
