import type { PadStatus } from "../types";
import { StatusBadge, type BadgeTone } from "./StatusBadge";

/** 状态条：三态走蓝 / 琥珀 / 绿，断开走灰，红色不进状态条（红色只留给急症与 SOS 事件） */
const STATUS_CONFIG: Record<
  PadStatus,
  { label: string; tone: BadgeTone; pulse: boolean }
> = {
  listening: { label: "正在听", tone: "info", pulse: true },
  thinking: { label: "正在想", tone: "warning", pulse: true },
  speaking: { label: "正在说", tone: "success", pulse: true },
  connecting: { label: "连接中", tone: "info", pulse: true },
  disconnected: { label: "已断开", tone: "neutral", pulse: false },
};

/** 状态条：PRD FR-UI-02 口径（触控区 ≥48px 正文 ≥20px），文案不自创交互 */
export function StatusBar({ status }: { status: PadStatus }) {
  const cfg = STATUS_CONFIG[status];
  return (
    <div aria-live="polite">
      <StatusBadge tone={cfg.tone} pulse={cfg.pulse} size="lg">
        {cfg.label}
      </StatusBadge>
    </div>
  );
}
