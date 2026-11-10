import { HealthCheckup } from "../pages/HealthCheckup";
import { IdentityConfirm } from "../pages/IdentityConfirm";
import { MedicationCard } from "../pages/MedicationCard";
import { MOCK_CANDIDATES, MOCK_MEDICATION } from "../tasks";
import type { PadTaskCommand } from "../task-channel";

/**
 * 任务命令 → 面板：主屏与三个原型面板之间唯一的适配层。
 *
 * - key=taskId：后端重复下发同一 taskId 不重置本地交互状态（FR-MED-04 幂等口径）
 * - 载荷缺失时回落教学模拟数据，不猜业务事实、不发起请求
 * - 面板反馈话术经 onHint 写大字幕 notice 区；原型期没有 TTS，不伪装成 reply
 */
interface TaskPanelProps {
  command: PadTaskCommand;
  onExit: () => void;
  onHint: (text: string) => void;
  /** Day 7 L6#8：任务卡片按钮的文本上行（用药确认等），由 App 接到 user_text 发送。 */
  onAnswer?: (text: string) => void;
}

export function TaskPanel({ command, onExit, onHint, onAnswer }: TaskPanelProps) {
  const payload = command.payload;

  switch (command.kind) {
    case "medication": {
      const snapshot =
        payload?.kind === "medication" ? payload.snapshot : MOCK_MEDICATION;
      return (
        <MedicationCard
          key={command.taskId}
          data={snapshot}
          onExit={onExit}
          onSubtitle={onHint}
          onAnswer={onAnswer}
        />
      );
    }
    case "health": {
      const meta = payload?.kind === "health" ? payload : null;
      return (
        <HealthCheckup
          key={command.taskId}
          sourceLabel={meta ? `${meta.startedAt} 定时` : undefined}
          onExit={onExit}
          onSubtitle={onHint}
        />
      );
    }
    case "identity": {
      const meta = payload?.kind === "identity" ? payload : null;
      return (
        <IdentityConfirm
          key={command.taskId}
          candidates={meta?.candidates ?? MOCK_CANDIDATES}
          reason={meta?.reason}
          onExit={onExit}
          onSubtitle={onHint}
        />
      );
    }
  }
}
