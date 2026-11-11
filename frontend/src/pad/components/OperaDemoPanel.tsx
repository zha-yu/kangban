/**
 * 戏曲播放器的开发验收面板（`?demo=opera`，仅 DEV）。
 *
 * 为什么单独一块：WS 帧形状未定（Q-D4），接不了真实链路；但契约 §6.3 的七动作
 * 状态机可以先单端验。这块面板就是「单端调试」的载体——它只调 `OperaPlayer`，
 * 不碰 `protocol.ts`、不伪造帧类型。等 Q-D4 定了，把 `apply(action)` 接到真实帧
 * 上即可，面板可以整块删掉。
 *
 * 演示口径（对应 D4 验收「播放/暂停/打断演示留证」）：
 * ① 播放 → ② 暂停/继续 → ③ 重复一遍 → ④ 音量两档 → ⑤ 模拟播报打断（压住/恢复）
 * → ⑥ 停止。每一步状态都在面板上以文字体现，截图即可留证。
 */

import { useEffect, useMemo, useRef, useState } from "react";

import {
  OperaPlayer,
  type OperaAction,
  type OperaSnapshot,
  type OperaTrack,
} from "../audio/opera-player";

/** D4 真实链路：状态与动作都由 realtime（WS media_play/media_control）驱动 */
export interface OperaLiveProps {
  snapshot: OperaSnapshot;
  onAction: (action: OperaAction) => void;
}

/** 演示音源：3 秒五声音阶琶音（mp3，25KB），随仓库入库（不占 B4 的 static/media/opera） */
const DEMO_TRACK: OperaTrack = {
  mediaId: "demo-0000-0000-0000-000000000001",
  title: "演示曲·五声音阶（3 秒）",
  mediaUrl: "/media/opera/demo-tone.mp3",
  durationSec: 3,
};

const ACTIONS: { action: Parameters<OperaPlayer["apply"]>[0]; label: string }[] = [
  { action: "pause", label: "暂停" },
  { action: "resume", label: "继续" },
  { action: "stop", label: "停止" },
  { action: "next_track", label: "换一段" },
  { action: "volume_up", label: "大声点" },
  { action: "volume_down", label: "小声点" },
  { action: "replay", label: "重复一遍" },
];

const STATUS_TEXT: Record<OperaSnapshot["status"], string> = {
  idle: "待机",
  loading: "加载中",
  playing: "播放中",
  paused: "已暂停",
  ended: "播完",
};

export function OperaDemoPanel({ live }: { live?: OperaLiveProps }) {
  const playerRef = useRef<OperaPlayer | null>(null);
  const [demoSnapshot, setDemoSnapshot] = useState<OperaSnapshot>({
    status: "idle",
    track: null,
    volume: 0.8,
    positionSec: 0,
    ducked: false,
  });
  const [lastResult, setLastResult] = useState<string>("—");

  const player = useMemo(() => {
    if (live) return null; // 真实链路：播放器归 realtime，面板只渲染与发动作
    const instance = new OperaPlayer({
      onState: (s) => setDemoSnapshot({ ...s }),
      onError: (reason) => setLastResult(`失败：${reason}`),
    });
    playerRef.current = instance;
    return instance;
  }, [live]);

  useEffect(() => {
    if (!player) return;
    const timer = window.setInterval(() => setDemoSnapshot({ ...player.state }), 300);
    return () => {
      window.clearInterval(timer);
      player.dispose();
      playerRef.current = null;
    };
  }, [player]);

  const snapshot = live ? live.snapshot : demoSnapshot;

  async function run(action: OperaAction) {
    if (live) {
      live.onAction(action);
      setLastResult(`${action} → 已下发`);
      return;
    }
    // 「换一段」在只有一首演示曲时，用同一首模拟换源（真实链路由 media_service 给下一段）
    const next = action === "next_track" ? { ...DEMO_TRACK, title: `${DEMO_TRACK.title}·第 2 段` } : undefined;
    const result = await player!.apply(action, next);
    setLastResult(`${action} → ${result}`);
    setDemoSnapshot({ ...player!.state });
  }

  return (
    <section className="flex h-full w-full max-w-[1100px] flex-col gap-6 rounded-3xl bg-surface p-8 shadow-xl">
      <header className="flex items-baseline justify-between">
        <h1 className="text-3xl font-bold text-ink">戏曲播放器 · {live ? "实时" : "单端调试"}</h1>
        <span className="text-lg text-ink-muted">{live ? "media_play / media_control" : "?demo=opera（仅 DEV）"}</span>
      </header>

      <dl className="grid grid-cols-2 gap-4 text-xl">
        <div className="rounded-2xl bg-surface-muted px-5 py-4">
          <dt className="text-base text-ink-muted">状态</dt>
          <dd className="text-2xl font-semibold text-ink">
            {STATUS_TEXT[snapshot.status]}
            {snapshot.ducked ? "（被播报压住）" : ""}
          </dd>
        </div>
        <div className="rounded-2xl bg-surface-muted px-5 py-4">
          <dt className="text-base text-ink-muted">曲目</dt>
          <dd className="text-2xl font-semibold text-ink">{snapshot.track?.title ?? "—"}</dd>
        </div>
        <div className="rounded-2xl bg-surface-muted px-5 py-4">
          <dt className="text-base text-ink-muted">音量</dt>
          <dd className="text-2xl font-semibold text-ink">{Math.round(snapshot.volume * 100)}%</dd>
        </div>
        <div className="rounded-2xl bg-surface-muted px-5 py-4">
          <dt className="text-base text-ink-muted">上一个动作</dt>
          <dd className="text-2xl font-semibold text-ink">{lastResult}</dd>
        </div>
      </dl>

      <div className="flex flex-wrap gap-3">
        {!live && (
          <button
            type="button"
            className="rounded-2xl bg-brand px-6 py-4 text-2xl font-bold text-ink-inverse"
            onClick={async () => {
              const result = await player!.load(DEMO_TRACK);
              setLastResult(`load → ${result}`);
              setDemoSnapshot({ ...player!.state });
            }}
          >
            播放
          </button>
        )}
        {ACTIONS.map(({ action, label }) => (
          <button
            key={action}
            type="button"
            className="rounded-2xl bg-surface-muted px-6 py-4 text-2xl font-semibold text-ink active:bg-brand-strong active:text-ink-inverse"
            onClick={() => void run(action)}
          >
            {label}
          </button>
        ))}
      </div>

      {!live && (
        <div className="flex gap-3">
          <button
            type="button"
            className="rounded-2xl bg-warning-soft px-6 py-4 text-2xl font-semibold text-warning-ink"
            onClick={() => {
              player!.duckForSpeech();
              setLastResult("duckForSpeech（模拟开始说话）");
              setDemoSnapshot({ ...player!.state });
            }}
          >
            模拟开始说话（压住戏曲）
          </button>
          <button
            type="button"
            className="rounded-2xl bg-warning-soft px-6 py-4 text-2xl font-semibold text-warning-ink"
            onClick={() => {
              player!.resumeAfterSpeech();
              setLastResult("resumeAfterSpeech（播报结束）");
              setDemoSnapshot({ ...player!.state });
            }}
          >
            模拟说完（恢复戏曲）
          </button>
        </div>
      )}

      <p className="mt-auto text-lg leading-relaxed text-ink-muted">
        七动作来自照护台契约 §6.3；真实链路 `next_track` 由服务端随帧给下一段 URL。
        {live ? "播报/触达自动压住戏曲，播完恢复；开口即打断。" : "WS 帧形状未定（Q-D4），故本面板不接协议层。"}
      </p>
    </section>
  );
}
