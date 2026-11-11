import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";

/** SOS 请求三态：发送中 / 已生成事件 / 发送失败（可重试）。 */
export type SosStatus = "sending" | "sent" | "failed";

/**
 * SOS 求助结果态（FR-ALT-06、S5 演示主线）。
 *
 * 点击常显按钮即调 `POST /api/pad/sos` 生成敏感事件 + 护工工单，不经过语音路由；
 * 结果页只做告知，不提供「撤销」入口——红色事件不能由长者一键撤销（FR-UI-08）。
 */
export function SosResult({
  status,
  onRetry,
  onExit,
}: {
  status: SosStatus;
  onRetry: () => void;
  onExit: () => void;
}) {
  const sending = status === "sending";
  const failed = status === "failed";

  return (
    <TaskShell
      eyebrow="一键求助"
      title={sending ? "正在联系护工…" : failed ? "求助没有发出去" : "已通知护工，请稍等"}
      badge={
        <StatusBadge
          tone={sending ? "warning" : "danger"}
          size="md"
          pulse={sending}
        >
          {sending ? "正在发送" : failed ? "发送失败" : "求救事件已生成"}
        </StatusBadge>
      }
      footer={
        <div className="flex gap-4">
          {failed && (
            <button
              type="button"
              onClick={onRetry}
              className="h-24 flex-1 rounded-2xl bg-brand text-3xl font-semibold text-ink-inverse active:bg-brand-strong"
            >
              再试一次
            </button>
          )}
          <button
            type="button"
            onClick={onExit}
            className={`h-24 rounded-2xl border border-line bg-surface text-3xl font-semibold text-ink active:bg-surface-sunken ${
              failed ? "flex-1" : "w-full"
            }`}
          >
            返回主界面
          </button>
        </div>
      }
    >
      <div className="flex flex-col gap-5">
        <p className="text-4xl font-semibold leading-relaxed text-ink">
          {sending
            ? "请稍等，正在把求助发给负责您的护工。"
            : failed
              ? "请再按一次，或直接呼叫身边的护工。"
              : "负责您的护工和家属已经收到通知"}
        </p>
        <p className="text-2xl leading-relaxed text-ink-muted">
          {sending
            ? "发送成功后会在这里告诉您，不需要重复按。"
            : failed
              ? "如果按了多次仍显示失败，请直接呼叫工作人员，不要等待。"
              : "工作人员正在赶来。您可以坐下来等一会儿，不需要再操作。"}
        </p>
      </div>
      {!failed && (
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
      )}
      {!sending && !failed && (
        <p className="mt-8 text-xl text-ink-muted">
          如果情况有变化，可以直接告诉赶来的工作人员。
        </p>
      )}
      {/* 技术设计文档 §1.2 第 2 条：SOS 不替代急救，文案必须明确「非急救通道」 */}
      <p className="mt-4 text-xl font-semibold text-ink">
        这里不是急救通道；情况紧急请直接呼叫护工或拨打 120。
      </p>
    </TaskShell>
  );
}
