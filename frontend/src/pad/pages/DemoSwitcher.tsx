import type { TaskKind } from "../tasks";

/** 开发态任务适配器：本地开发模式常显（生产构建不含）。
 *  每次点击都 emitPadTask 造一条与后端同形的任务命令，走同一条任务总线，
 *  所以这里不只是「切页面」，也是任务下发契约的可运行样例。
 *  过去只在 ?mock=1 时出现，裸地址打开看不到任何新页面，故改为 DEV 常显。 */
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
  if (!LAYOUT_PREVIEW) return null;
  return (
    <div className="flex shrink-0 items-center gap-4 border-b border-line bg-surface-sunken px-12 py-3">
      <span className="text-xl font-semibold text-ink">页面布局</span>
      <div className="flex items-center gap-2">
        {ITEMS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => onChange(item.key)}
            className={`rounded-xl border px-6 py-3 text-xl font-semibold ${
              value === item.key
                ? "border-brand bg-brand text-ink-inverse"
                : "border-line bg-surface text-ink-muted active:bg-surface-sunken"
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>
      <span className="ml-auto text-lg text-ink-subtle">
        开发态任务下发 · 后端契约待接 · 数据为教学模拟 · 语音链路照常运行
      </span>
    </div>
  );
}
