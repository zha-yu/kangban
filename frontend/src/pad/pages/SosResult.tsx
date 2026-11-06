import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";

/**
 * SOS 求助结果态（FR-ALT-06、S5 演示主线）。
 *
 * 点击常显按钮即生成敏感事件 + 护工工单，不经过语音路由；
 * 结果页只做告知，不提供「撤销」入口——红色事件不能由长者一键撤销（FR-UI-08）。
 */
export function SosResult({ onExit }: { onExit: () => void }) {
  return (
    <TaskShell
      eyebrow="一键求助"
      title="已通知护工，请稍等"
      badge={
        <StatusBadge tone="danger" size="md">
          求救事件已生成
        </StatusBadge>
      }
      footer={
        <button
          type="button"
          onClick={onExit}
          className="h-24 w-full rounded-2xl border border-line bg-surface text-3xl font-semibold text-ink active:bg-surface-sunken"
        >
          返回主界面
        </button>
      }
    >
      <div className="flex flex-col gap-5">
        <p className="text-4xl font-semibold leading-relaxed text-ink">
          负责您的护工和家属已经收到通知
        </p>
        <p className="text-2xl leading-relaxed text-ink-muted">
          工作人员正在赶来。您可以坐下来等一会儿，不需要再操作。
        </p>
      </div>
      <div className="mt-10 grid grid-cols-3 gap-4 border-t border-line pt-8">
        {["求助已记录", "护工已通知", "家属已通知"].map((text) => (
          <div
            key={text}
            className="rounded-2xl border border-line bg-surface-sunken px-5 py-6 text-center text-2xl text-ink-muted"
          >
            {text}
          </div>
        ))}
      </div>
      <p className="mt-8 text-xl text-ink-muted">
        如果情况有变化，可以直接告诉赶来的工作人员。
      </p>
    </TaskShell>
  );
}
