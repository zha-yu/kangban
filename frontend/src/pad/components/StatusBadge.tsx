import type { ReactNode } from "react";

/**
 * 状态徽标：信号色的唯一合法载体（100 级淡底 + 700 级深字深点）。
 *
 * 约定（见 docs/前端页面方案.md）：
 *  - 颜色退成第三线索，文字标签与状态点始终在场，不靠单纯的颜色区分等级
 *  - 只做小面积；不铺面、不做按钮底、不用于 60px 大字幕
 *  - 红色（danger）只留给急症 / 红色事件 / 表单错误，不用于"已断开"
 */
export type BadgeTone = "danger" | "warning" | "success" | "info" | "neutral";

const TONE_STYLE: Record<BadgeTone, string> = {
  danger: "bg-danger-soft text-danger-ink",
  warning: "bg-warning-soft text-warning-ink",
  success: "bg-success-soft text-success-ink",
  info: "bg-info-soft text-info-ink",
  neutral: "bg-neutral-soft text-neutral-ink",
};

const SIZE_STYLE = {
  sm: "gap-1.5 rounded-lg px-2.5 py-1 text-base",
  md: "gap-2 rounded-xl px-4 py-2 text-xl",
  lg: "gap-3 rounded-2xl px-6 py-3 text-3xl",
} as const;

const DOT_STYLE = {
  sm: "h-2 w-2",
  md: "h-2.5 w-2.5",
  lg: "h-4 w-4",
} as const;

interface StatusBadgeProps {
  tone: BadgeTone;
  children: ReactNode;
  /** 状态点，默认显示；进行中状态可开 pulse */
  dot?: boolean;
  pulse?: boolean;
  size?: keyof typeof SIZE_STYLE;
  className?: string;
}

export function StatusBadge({
  tone,
  children,
  dot = true,
  pulse = false,
  size = "md",
  className = "",
}: StatusBadgeProps) {
  return (
    <span
      role="status"
      className={`inline-flex items-center font-semibold ${SIZE_STYLE[size]} ${TONE_STYLE[tone]} ${className}`}
    >
      {dot && (
        <span
          aria-hidden="true"
          className={`shrink-0 rounded-full bg-current ${DOT_STYLE[size]} ${pulse ? "animate-pulse" : ""}`}
        />
      )}
      {children}
    </span>
  );
}
