import { useState } from "react";

import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";

/** SOS 请求三态：发送中 / 已生成事件 / 发送失败（可重试）。 */
export type SosStatus = "sending" | "sent" | "failed";

/**
 * SOS 求助结果态（FR-ALT-06、S5 演示主线；Day 9 F03 真实状态驱动）。
 *
 * 点击常显按钮即调 `POST /api/pad/sos` 生成敏感事件 + 护工工单，不经过语音路由；
 * 结果页三格只陈述**后端真实返回**的事实：事件已生成、工单是否已创建（orderId 有无）。
 * `status === "sent"` 只代表「事件 + 工单已生成」，**不代表护工/家属已收到通知**——
 * 本页任何状态都不得出现「已通知」断言（任务书 F03 / AGENTS §4.4：断开通知出口
 * 后按 SOS，页面不得出现「已通知」）。
 * 结果页不提供「撤销」入口——红色事件不能由长者一键撤销（FR-UI-08），
 * 只允许追加「说错了」补充说明，事件保持 open（AC-26）。
 */
export function SosResult({
  status,
  orderId,
  onRetry,
  onCorrection,
  onExit,
}: {
  status: SosStatus;
  /** 后端 SOS 响应的 order_id（真实返回）；null = 工单未创建（如无责任护工绑定），照实陈述 */
  orderId?: string | null;
  onRetry: () => void;
  /** 追加「说错了」说明；未拿到 event_id（如 ?sos=1 直达）时不传，按钮隐藏。 */
  onCorrection?: () => Promise<boolean>;
  onExit: () => void;
}) {
  const sending = status === "sending";
  const failed = status === "failed";
  const [corrected, setCorrected] = useState(false);
  const [correcting, setCorrecting] = useState(false);

  async function handleCorrection() {
    if (!onCorrection || correcting) return;
    setCorrecting(true);
    const ok = await onCorrection();
    setCorrecting(false);
    if (ok) setCorrected(true);
  }

  return (
    <TaskShell
      eyebrow="一键求助"
      title={sending ? "正在联系护工…" : failed ? "求助没有发出去" : "求助已发出，正在通知护工"}
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
              : "求助已记录，正在通知负责您的护工和家属。"}
        </p>
        <p className="text-2xl leading-relaxed text-ink-muted">
          {sending
            ? "发送成功后会在这里告诉您，不需要重复按。"
            : failed
              ? "如果按了多次仍显示失败，请直接呼叫工作人员，不要等待。"
              : "工作人员收到通知后会赶来。您可以坐下来等一会儿，不需要再操作。"}
        </p>
      </div>
      {!failed && (
        <div className="mt-10 grid grid-cols-3 gap-4 border-t border-line pt-8">
          {/* 三格 = 后端真实状态，逐格陈述事实，不做「已通知」断言（Day 9 F03） */}
          {[
            "求助已记录",
            orderId ? "工单已创建" : "正在分派护工",
            "通知发出后护工会赶来",
          ].map((text) => (
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
      {!sending && !failed && onCorrection && (
        <div className="mt-4">
          {corrected ? (
            <p className="text-xl text-ink-muted">已记录您的补充说明，工作人员会来核实。</p>
          ) : (
            <button
              type="button"
              onClick={() => void handleCorrection()}
              className="h-16 rounded-2xl border border-line bg-surface px-6 text-2xl text-ink-muted active:bg-surface-sunken"
            >
              {correcting ? "正在提交…" : "我说错了（补充说明）"}
            </button>
          )}
        </div>
      )}
      {/* 技术设计文档 §1.2 第 2 条：SOS 不替代急救，文案必须明确「非急救通道」 */}
      <p className="mt-4 text-xl font-semibold text-ink">
        这里不是急救通道；情况紧急请直接呼叫护工或拨打 120。
      </p>
    </TaskShell>
  );
}
