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

import { useEffect, useState } from "react";

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

/** 底部播放条按用途分三组：七动作一个不少，只是不再等权平铺成一条长龙 */
const COMPACT_GROUPS: { key: string; actions: string[] }[] = [
  { key: "播放", actions: ["pause", "resume", "stop"] },
  { key: "曲目", actions: ["next_track", "replay"] },
  { key: "音量", actions: ["volume_up", "volume_down"] },
];

export function OperaDemoPanel({
  live,
  compact = false,
  onCollapse,
}: {
  live?: OperaLiveProps;
  /** 底部播放条形态（主界面下方常驻）：信息行 + 分组动作行两行结构；仅 live 模式支持 */
  compact?: boolean;
  /** 由主屏注入的收起回调；放在信息行右端，不再用绝对定位浮层盖住按钮 */
  onCollapse?: () => void;
}) {
  const [demoSnapshot, setDemoSnapshot] = useState<OperaSnapshot>({
    status: "idle",
    track: null,
    volume: 0.8,
    positionSec: 0,
    ducked: false,
  });
  const [lastResult, setLastResult] = useState<string>("—");
  /**
   * 播放器由 effect 创建/销毁（不是 useMemo）：DEV 的 StrictMode 会「挂载→清理→再挂载」，
   * useMemo 实例在清理里被 dispose 后不会重建，面板从此全部动作返回 ignored（状态永远待机）。
   */
  const [player, setPlayer] = useState<OperaPlayer | null>(null);

  useEffect(() => {
    if (live) return; // 真实链路：播放器归 realtime，面板只渲染与发动作
    const instance = new OperaPlayer({
      onState: (s) => setDemoSnapshot({ ...s }),
      onError: (reason) => setLastResult(`失败：${reason}`),
    });
    setPlayer(instance);
    const timer = window.setInterval(() => setDemoSnapshot({ ...instance.state }), 300);
    return () => {
      window.clearInterval(timer);
      instance.dispose();
      setPlayer(null);
    };
  }, [live]);

  const snapshot = live ? live.snapshot : demoSnapshot;

  async function run(action: OperaAction) {
    if (live) {
      live.onAction(action);
      setLastResult(`${action} → 已下发`);
      return;
    }
    if (!player) return; // effect 尚未建好（首帧），忽略本次点击
    // 「换一段」在只有一首演示曲时，用同一首模拟换源（真实链路由 media_service 给下一段）
    const next = action === "next_track" ? { ...DEMO_TRACK, title: `${DEMO_TRACK.title}·第 2 段` } : undefined;
    const result = await player.apply(action, next);
    setLastResult(`${action} → ${result}`);
    setDemoSnapshot({ ...player.state });
  }

  // 底部播放条：两行结构。旧版把「徽标 + 曲名 + 长状态串 + 7 个按钮」全塞进一行，
  // 左右两侧都是 shrink-0，窄视口下按钮组横向溢出、盖住状态文字并超出卡片右边缘；
  // 「收起」还是绝对定位浮层，又压在最后一个按钮上。现在信息行与动作行分离，
  // 曲名可截断、动作按用途分三组且允许换行，收起按钮归位到信息行右端。
  if (compact && live) {
    return (
      <section
        aria-label="戏曲播放条"
        className="flex w-full flex-col gap-3 rounded-3xl border border-line bg-surface px-6 py-4 shadow-xl"
      >
        {/* 信息行：曲名是唯一可伸缩项，长歌名截断而不再把状态与按钮顶出卡片 */}
        <div className="flex min-w-0 items-center gap-4">
          <span className="shrink-0 rounded-control bg-brand-soft px-3 py-1 text-base font-semibold text-brand-strong">
            实时
          </span>
          <span
            className="min-w-0 flex-1 truncate text-2xl font-semibold text-ink"
            title={snapshot.track?.title ?? undefined}
          >
            {snapshot.track?.title ?? "—"}
          </span>
          <span className="shrink-0 whitespace-nowrap text-lg text-ink-muted">
            {STATUS_TEXT[snapshot.status]}
            {snapshot.ducked ? " · 播报中已压低" : ""} · 音量{" "}
            {Math.round(snapshot.volume * 100)}%
          </span>
          {onCollapse && (
            <button
              type="button"
              aria-label="收起播放面板"
              onClick={onCollapse}
              className="shrink-0 rounded-control border border-line bg-surface px-4 py-2 text-lg text-ink-muted hover:bg-surface-sunken focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand active:bg-brand-soft"
            >
              收起
            </button>
          )}
        </div>

        {/* 动作行：播放 / 曲目 / 音量三组，组间竖线分隔；窄视口自动换行不重叠 */}
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          {COMPACT_GROUPS.map((group, index) => (
            <div
              key={group.key}
              aria-label={`${group.key}控制`}
              className={`flex flex-wrap items-center gap-2 ${index > 0 ? "border-l border-line pl-5" : ""}`}
            >
              {ACTIONS.filter((item) => group.actions.includes(item.action)).map(
                ({ action, label }) => (
                  <button
                    key={action}
                    type="button"
                    className="inline-flex min-h-12 shrink-0 items-center justify-center whitespace-nowrap rounded-control border border-line bg-surface-sunken px-5 text-xl font-semibold text-ink hover:bg-brand-soft focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand active:bg-brand-strong active:text-ink-inverse"
                    onClick={() => void run(action)}
                  >
                    {label}
                  </button>
                ),
              )}
            </div>
          ))}
        </div>
      </section>
    );
  }

  // 过渡占位件：高度由内容决定，不再 h-full 撑满主区；超高时卡内滚动，
  // 避免旧版 mt-auto 把开发说明推到卡底、中间空掉一大片
  return (
    <section className="flex max-h-full w-full max-w-[1100px] flex-col gap-6 overflow-y-auto rounded-3xl bg-surface p-8 shadow-xl">
      <header className="flex items-center justify-between gap-6">
        <h1 className="text-3xl font-bold text-ink">戏曲播放器</h1>
        {/* 技术帧名不进首屏，折进底部「开发信息」；这里只留模式徽标 */}
        <span className="shrink-0 rounded-control bg-brand-soft px-3 py-1 text-base font-semibold text-brand-strong">
          {live ? "实时" : "单端调试"}
        </span>
      </header>

      <dl className="grid grid-cols-2 gap-4 text-xl">
        <div className="rounded-2xl bg-surface-sunken px-5 py-4">
          <dt className="text-base text-ink-muted">状态</dt>
          <dd className="text-2xl font-semibold text-ink">
            {STATUS_TEXT[snapshot.status]}
            {snapshot.ducked ? "（被播报压住）" : ""}
          </dd>
        </div>
        <div className="rounded-2xl bg-surface-sunken px-5 py-4">
          <dt className="text-base text-ink-muted">曲目</dt>
          <dd className="text-2xl font-semibold text-ink">{snapshot.track?.title ?? "—"}</dd>
        </div>
        <div className="rounded-2xl bg-surface-sunken px-5 py-4">
          <dt className="text-base text-ink-muted">音量</dt>
          <dd className="text-2xl font-semibold text-ink">{Math.round(snapshot.volume * 100)}%</dd>
        </div>
        <div className="rounded-2xl bg-surface-sunken px-5 py-4">
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
              if (!player) return;
              const result = await player.load(DEMO_TRACK);
              setLastResult(`load → ${result}`);
              setDemoSnapshot({ ...player.state });
            }}
          >
            播放
          </button>
        )}
        {ACTIONS.map(({ action, label }) => (
          <button
            key={action}
            type="button"
            className="rounded-2xl border border-line bg-surface-sunken px-6 py-4 text-2xl font-semibold text-ink hover:bg-brand-soft focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand active:bg-brand-strong active:text-ink-inverse"
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
              if (!player) return;
              player.duckForSpeech();
              setLastResult("duckForSpeech（模拟开始说话）");
              setDemoSnapshot({ ...player.state });
            }}
          >
            模拟开始说话（压住戏曲）
          </button>
          <button
            type="button"
            className="rounded-2xl bg-warning-soft px-6 py-4 text-2xl font-semibold text-warning-ink"
            onClick={() => {
              if (!player) return;
              player.resumeAfterSpeech();
              setLastResult("resumeAfterSpeech（播报结束）");
              setDemoSnapshot({ ...player.state });
            }}
          >
            模拟说完（恢复戏曲）
          </button>
        </div>
      )}

      {/* 技术帧名与契约编号默认折叠：这是 DEV 验收件，不占产品首屏 */}
      <details className="shrink-0 rounded-control bg-surface-sunken px-5 py-3 text-base text-ink-muted">
        <summary className="cursor-pointer font-semibold text-ink-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand">
          开发信息 · 帧类型与契约口径
        </summary>
        <div className="mt-3 flex flex-col gap-2 leading-relaxed">
          <p>
            帧类型：<span className="font-mono">media_play / media_control</span>
            {live ? "" : "（当前为 ?demo=opera 单端调试，不接协议层）"}
          </p>
          <p>
            七动作来自照护台契约 §6.3；真实链路 next_track 由服务端随帧给下一段 URL。
            {live ? "播报/触达自动压住戏曲，播完恢复；开口即打断。" : "WS 帧形状未定（Q-D4），故本面板不接协议层。"}
          </p>
        </div>
      </details>
    </section>
  );
}
