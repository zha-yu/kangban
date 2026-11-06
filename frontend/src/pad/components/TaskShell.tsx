import type { ReactNode } from "react";

/**
 * 任务面板通用外壳：主对话屏中部右侧的任务区。
 *
 * 布局口径：任务来时不整屏切页，顶栏状态条与底部大字幕保持在场
 * （S3「语音 + 字幕 + 按钮同时呈现」、FR-UI-02 字幕与音频一致）。
 * 高度固定，内容超长时只让正文区滚动，标题与底部动作永远可见。
 */
interface TaskShellProps {
  eyebrow: string;
  title: string;
  /** 信号色的唯一入口：StatusBadge，不铺面 */
  badge?: ReactNode;
  progress?: ReactNode;
  exitLabel?: string;
  onExit?: () => void;
  children: ReactNode;
  footer?: ReactNode;
}

export function TaskShell({
  eyebrow,
  title,
  badge,
  progress,
  exitLabel = "退出任务",
  onExit,
  children,
  footer,
}: TaskShellProps) {
  return (
    <section
      className="flex h-full max-h-[660px] w-[960px] max-w-[60vw] shrink-0 flex-col overflow-hidden rounded-2xl border border-line bg-surface p-8"
    >
      <header className="flex shrink-0 items-start justify-between gap-8">
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-4">
            <span className="text-xl font-semibold text-ink-muted">{eyebrow}</span>
            {badge}
          </div>
          <h2 className="text-5xl font-bold leading-tight text-ink">{title}</h2>
        </div>
        {onExit && (
          <button
            type="button"
            onClick={onExit}
            className="shrink-0 rounded-2xl border border-line bg-surface px-6 py-4 text-xl text-ink-muted active:bg-surface-sunken"
          >
            {exitLabel}
          </button>
        )}
      </header>

      {progress && <div className="mt-6 shrink-0">{progress}</div>}

      <div className="mt-6 flex min-h-0 flex-1 flex-col overflow-y-auto">
        {children}
      </div>

      {footer && (
        <div className="mt-6 shrink-0 border-t border-line pt-6">{footer}</div>
      )}
    </section>
  );
}
