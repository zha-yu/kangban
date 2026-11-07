/**
 * SOS 常显大按钮（FR-ALT-06）。
 *
 * 放在顶栏最右侧，形象选择页由父级注入同一位置（headerExtra），
 * 相位切换、任务面板、SOS 结果态都不改变它的位置（v1.4 接回）；
 * 实心主色 + 大字「一键求助」，不使用红色小块，避免与「红色事件」状态混淆
 * （见 docs/前端页面方案.md §2 规则 5）。触控区 80px 高，远超 PRD 的 ≥48×48px 门槛。
 */
interface SosButtonProps {
  onClick: () => void;
  active?: boolean;
}

export function SosButton({ onClick, active = false }: SosButtonProps) {
  return (
    <button
      type="button"
      aria-label="一键求助"
      aria-pressed={active}
      onClick={onClick}
      className={`flex h-20 shrink-0 items-center gap-3 rounded-2xl px-8 text-3xl font-bold text-ink-inverse ${
        active ? "bg-brand-strong" : "bg-brand active:bg-brand-strong"
      }`}
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="h-9 w-9"
      >
        <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
        <path d="M13.7 21a2 2 0 0 1-3.4 0" />
      </svg>
      一键求助
    </button>
  );
}
