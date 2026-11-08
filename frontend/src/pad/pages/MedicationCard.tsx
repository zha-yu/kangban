import { useEffect, useState } from "react";
import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";
import type { MedicationSnapshot } from "../tasks";

/**
 * 用药提醒卡片（PRD §4.5、FR-UI-03/04/08、FR-MED-01/02/06）。
 *
 * 布局要点：文字按钮与语音同时可用（麦克风被拒也能确认）；播报可一键打断；
 * 普通确认 10 秒内可撤销；挂载即触发 card_rendered 回执（此处只留回调，不发起请求）。
 * 拒服不铺红底——红色只留给急症 / 红色事件。
 */
type MedState = "pending" | "taken" | "refused";

interface MedicationCardProps {
  data: MedicationSnapshot;
  onExit: () => void;
  onRendered?: () => void;
  onSubtitle?: (text: string) => void;
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-base text-ink-muted">{label}</span>
      <span className="text-xl font-semibold text-ink">{value}</span>
    </div>
  );
}

export function MedicationCard({
  data,
  onExit,
  onRendered,
  onSubtitle,
}: MedicationCardProps) {
  const [state, setState] = useState<MedState>("pending");
  const [remaining, setRemaining] = useState(data.windowSeconds);
  const [undoLeft, setUndoLeft] = useState(10);
  const [interrupted, setInterrupted] = useState(false);

  // 页面成功渲染即回执：由调用方保证幂等（FR-MED-02 / FR-ALT-03「只有客户端回执算送达」）
  useEffect(() => {
    onRendered?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 每次触达独立 60 秒确认窗口
  useEffect(() => {
    if (state !== "pending") return;
    const timer = setInterval(
      () => setRemaining((v) => Math.max(0, v - 1)),
      1000,
    );
    return () => clearInterval(timer);
  }, [state]);

  // 普通确认 10 秒可撤销；红色事件不允许长者一键撤销，本卡片不提供该入口
  useEffect(() => {
    if (state !== "taken") return;
    const timer = setInterval(
      () => setUndoLeft((v) => Math.max(0, v - 1)),
      1000,
    );
    return () => clearInterval(timer);
  }, [state]);

  function handleTaken() {
    setState("taken");
    setUndoLeft(10);
    onSubtitle?.("好的，已记下您说吃过药了");
  }

  function handleRefused() {
    setState("refused");
    onSubtitle?.("好的，这次先不吃，我会告诉护工");
  }

  return (
    <TaskShell
      eyebrow="用药提醒"
      title="该吃药了"
      badge={
        <StatusBadge tone="warning" size="md">
          第 {data.attemptNo} 次提醒
        </StatusBadge>
      }
      onExit={onExit}
      exitLabel="稍后再说"
      footer={
        <div className="flex items-stretch gap-4">
          {state === "pending" && (
            <>
              <button
                type="button"
                onClick={handleTaken}
                className="h-20 flex-1 rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
              >
                已服用
              </button>
              <button
                type="button"
                onClick={handleRefused}
                className="h-20 rounded-2xl border border-line bg-surface px-10 text-2xl font-semibold text-ink active:bg-surface-sunken"
              >
                这次不吃
              </button>
            </>
          )}
          {state === "taken" && (
            <>
              {undoLeft > 0 ? (
                <button
                  type="button"
                  onClick={() => setState("pending")}
                  className="h-20 flex-1 rounded-2xl border border-line bg-surface text-2xl font-semibold text-ink active:bg-surface-sunken"
                >
                  撤销（{undoLeft} 秒内）
                </button>
              ) : (
                <span className="flex flex-1 items-center text-2xl text-ink-muted">
                  已记录，撤销时间已过
                </span>
              )}
              <button
                type="button"
                onClick={onExit}
                className="h-20 rounded-2xl bg-brand px-12 text-2xl font-bold text-ink-inverse active:bg-brand-strong"
              >
                完成
              </button>
            </>
          )}
          {state === "refused" && (
            <button
              type="button"
              onClick={onExit}
              className="h-20 w-full rounded-2xl bg-brand text-2xl font-bold text-ink-inverse active:bg-brand-strong"
            >
              返回主界面
            </button>
          )}
        </div>
      }
    >
      {/* 药品快照：历史提醒保留旧值，不随药单修改变化 */}
      <div className="rounded-2xl border border-line bg-surface-sunken p-5">
        <p className="text-base text-ink-muted">本次提醒的药品</p>
        <p className="mt-2 text-3xl font-bold leading-tight text-ink">
          {data.drugName}
        </p>
        <div className="mt-4 grid grid-cols-3 gap-4 border-t border-line pt-4">
          <Info label="剂量" value={data.dose} />
          <Info label="计划时间" value={data.scheduledAt} />
          <Info label="服用方式" value={data.usage} />
        </div>
      </div>

      {/* 状态行：信号色只出现在徽标上；打断播报是播报控制，不与确认动作混排 */}
      <div className="mt-4 flex items-center justify-between gap-6">
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge tone="success" size="sm">
            卡片已送达
          </StatusBadge>
          {state === "pending" && (
            <StatusBadge tone={remaining <= 10 ? "warning" : "neutral"} size="sm">
              确认剩余 {remaining} 秒
            </StatusBadge>
          )}
          {state === "taken" && (
            <StatusBadge tone="success" size="sm">
              已记录 · 已服自报
            </StatusBadge>
          )}
          {state === "refused" && (
            <StatusBadge tone="warning" size="sm">
              已记录拒服 · 护工会来确认
            </StatusBadge>
          )}
        </div>
        <button
          type="button"
          disabled={interrupted}
          onClick={() => {
            setInterrupted(true);
            onSubtitle?.("已停止播报，提醒仍可确认");
          }}
          className="shrink-0 rounded-2xl border border-line bg-surface px-5 py-3 text-lg text-ink-muted active:bg-surface-sunken disabled:opacity-50"
        >
          {interrupted ? "已打断播报" : "打断播报"}
        </button>
      </div>

      <p className="mt-3 text-base text-ink-muted">
        {data.snapshotAt} · 第 {data.attemptNo} 次提醒 · 也可以直接说「吃了」
        {state === "taken" && undoLeft > 0 && " · 10 秒内可撤销"}
      </p>
    </TaskShell>
  );
}
