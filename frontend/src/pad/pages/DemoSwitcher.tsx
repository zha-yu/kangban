import { useEffect, useRef, useState } from "react";
import type { TaskKind } from "../tasks";

/** 开发态任务适配器：本地开发模式常显（生产构建不含）。
 *  每次点击都 emitPadTask 造一条与后端同形的任务命令，走同一条任务总线，
 *  所以这里不只是「切页面」，也是任务下发契约的可运行样例。
 *
 *  形态（v1.6）：固定定位的一枚小 chip + 展开后的浮层面板，不进文档流。
 *  旧版是顶部一整条 20px 大按钮横排，在 1032px 视口里被压到中文竖排，
 *  且占了主屏最显眼的位置；现在四段式骨架不再被开发工具顶掉，
 *  小字号也明确区分「给演示者用的」与「给长者用的」。 */
const LAYOUT_PREVIEW = import.meta.env.DEV;

const ITEMS: { key: TaskKind; label: string }[] = [
  { key: "none", label: "主对话" },
  { key: "medication", label: "用药提醒" },
  { key: "health", label: "健康 5 问" },
  { key: "identity", label: "身份确认" },
  { key: "sos", label: "SOS 结果" },
];

interface DemoSwitcherProps {
  value: TaskKind;
  onChange: (task: TaskKind) => void;
}

export function DemoSwitcher({ value, onChange }: DemoSwitcherProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const current = ITEMS.find((item) => item.key === value)?.label ?? "主对话";

  // Esc / 点面板外收起；仅在展开时挂监听
  useEffect(() => {
    if (!LAYOUT_PREVIEW || !open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onPointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open]);

  if (!LAYOUT_PREVIEW) return null;

  return (
    <div
      ref={rootRef}
      className="fixed bottom-5 left-5 z-50 flex flex-col items-start gap-2"
    >
      {open && (
        <section
          aria-label="演示页面"
          className="w-64 max-w-[calc(100vw-2.5rem)] rounded-2xl border border-line bg-surface p-3 shadow-lg"
        >
          <div className="flex items-baseline justify-between px-2 pb-2">
            <h2 className="text-base font-semibold text-ink">演示页面</h2>
            <span className="text-xs text-ink-subtle">开发态</span>
          </div>
          <ul className="flex flex-col gap-1">
            {ITEMS.map((item) => {
              const selected = value === item.key;
              return (
                <li key={item.key}>
                  <button
                    type="button"
                    onClick={() => {
                      onChange(item.key);
                      setOpen(false);
                    }}
                    aria-current={selected ? "true" : undefined}
                    className={`flex w-full items-center justify-between whitespace-nowrap rounded-control px-3 py-2 text-left text-base focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand ${
                      selected
                        ? "bg-brand-soft font-semibold text-brand"
                        : "text-ink-muted hover:bg-surface-sunken active:bg-surface-sunken"
                    }`}
                  >
                    <span>{item.label}</span>
                    {selected && (
                      <span className="text-xs font-normal text-brand">当前</span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
          <p className="mt-2 border-t border-line px-2 pt-2 text-xs leading-relaxed text-ink-subtle">
            任务经总线以开发命令下发，数据为教学模拟；后端契约待接，语音链路照常运行。
          </p>
        </section>
      )}

      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="flex items-center gap-2 rounded-control border border-line bg-surface px-3 py-2 text-base shadow-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand active:bg-surface-sunken"
      >
        <span className="h-2 w-2 shrink-0 rounded-full bg-brand" />
        <span className="font-semibold text-ink">演示</span>
        <span className="text-ink-subtle">·</span>
        <span className="whitespace-nowrap text-ink-muted">{current}</span>
        <svg
          aria-hidden="true"
          viewBox="0 0 12 12"
          className={`h-3 w-3 text-ink-subtle transition-transform duration-150 ${open ? "rotate-180" : ""}`}
        >
          <path
            d="M2 4.5 6 8.5 10 4.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
    </div>
  );
}
