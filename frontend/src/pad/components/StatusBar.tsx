import type { PadStatus } from "../types";

const STATUS_CONFIG: Record<
  PadStatus,
  { label: string; dot: string; bg: string }
> = {
  listening: { label: "正在听", dot: "bg-emerald-400", bg: "bg-emerald-500/15" },
  thinking: { label: "正在想", dot: "bg-amber-400", bg: "bg-amber-500/15" },
  speaking: { label: "正在说", dot: "bg-sky-400", bg: "bg-sky-500/15" },
  connecting: { label: "连接中", dot: "bg-slate-400", bg: "bg-slate-500/15" },
  disconnected: { label: "已断开", dot: "bg-red-400", bg: "bg-red-500/15" },
};

/** 状态条：PRD FR-UI-02 口径（触控区 ≥48px 正文 ≥20px），文案不自创交互 */
export function StatusBar({ status }: { status: PadStatus }) {
  const cfg = STATUS_CONFIG[status];
  return (
    <div
      className={`flex items-center gap-3 rounded-2xl px-6 py-3 ${cfg.bg}`}
      aria-live="polite"
    >
      <span className={`h-4 w-4 rounded-full ${cfg.dot} animate-pulse`} />
      <span className="text-3xl font-semibold tracking-wide">{cfg.label}</span>
    </div>
  );
}
