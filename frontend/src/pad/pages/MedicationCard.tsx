import { useEffect, useState } from "react";
import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";
import { revokeTakenReport, MedicationUndoError } from "../api";
import { loadSession } from "../session";
import type { MedicationSnapshot } from "../tasks";

/**
 * 用药提醒卡片（PRD §4.5、FR-UI-03/04/08、FR-MED-01/02/06）。
 *
 * 布局要点：文字按钮与语音同时可用（麦克风被拒也能确认）；播报可一键打断；
 * 普通确认 10 秒内可撤销——撤销**调后端**（POST /api/pad/medication-events/{id}/revoke，
 * Day 9 F24），成功才回滚 UI，失败显示可重试、绝不误显示已撤销（FR-UI-08）；
 * 挂载即触发 card_rendered 回执（经 onRendered 由调用方发 client_receipt 帧，Day 9 X1）。
 * 拒服不铺红底——红色只留给急症 / 红色事件。
 */
type MedState = "pending" | "taken" | "refused";
type UndoState = "idle" | "sending" | "failed";

interface MedicationCardProps {
  data: MedicationSnapshot;
  onExit: () => void;
  onRendered?: () => void;
  onSubtitle?: (text: string) => void;
  /** Day 7 L6#8：按钮确认上行（「已服用」→「吃过了」/「这次不吃」→「不吃」，
   *  均为后端用药确认词表内口语，经 user_text 按 ASR FINAL 同路进编排）。 */
  onAnswer?: (text: string) => void;
  /** Day 9：触达任务 id（= medication_events.reminder_id），撤销端点路径参数；
   *  开发态 mock 任务无真实事件，撤销会 404/422 → 走失败可重试分支（诚实呈现）。 */
  taskId?: string;
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
  onAnswer,
  taskId,
}: MedicationCardProps) {
  const [state, setState] = useState<MedState>("pending");
  const [remaining, setRemaining] = useState(data.windowSeconds);
  const [undoLeft, setUndoLeft] = useState(10);
  const [undoState, setUndoState] = useState<UndoState>("idle");
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
    onAnswer?.("吃过了"); // 词表内口语（_TAKEN_WORDS），经 user_text 进编排
    onSubtitle?.("好的，已记下您说吃过药了");
  }

  function handleRefused() {
    setState("refused");
    onAnswer?.("不吃"); // 词表内口语（_REFUSED_WORDS）
    onSubtitle?.("好的，这次先不吃，我会告诉护工");
  }

  /** Day 9 F24：撤销调后端既有端点，成功后才回滚 UI（FR-UI-08）。
   *  失败不吞——UI 停留在「已记录」态并显示可重试，绝不误显示已撤销。 */
  async function handleUndo() {
    if (!taskId || undoState === "sending") return;
    setUndoState("sending");
    const token = loadSession()?.access_token;
    if (!token) {
      setUndoState("failed");
      return;
    }
    try {
      await revokeTakenReport(token, taskId);
      setUndoState("idle");
      setState("pending");
      setRemaining(data.windowSeconds);
      onSubtitle?.("好的，已撤销刚才的记录，请您再确认一次");
    } catch (err) {
      // 窗口已过（409 UNDO_WINDOW_EXPIRED）不再提供重试——由外层 undoLeft 计时自然收口
      if (err instanceof MedicationUndoError && err.code === "window_expired") {
        setUndoState("idle");
        onSubtitle?.("撤销时间已过，护工会来确认");
        return;
      }
      setUndoState("failed");
    }
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
              {undoLeft > 0 && undoState !== "failed" ? (
                <button
                  type="button"
                  onClick={() => void handleUndo()}
                  disabled={undoState === "sending"}
                  className="h-20 flex-1 rounded-2xl border border-line bg-surface text-2xl font-semibold text-ink active:bg-surface-sunken disabled:opacity-50"
                >
                  {undoState === "sending"
                    ? "正在撤销…"
                    : `撤销（${undoLeft} 秒内）`}
                </button>
              ) : undoLeft > 0 && undoState === "failed" ? (
                /* Day 9 F24：撤销失败可重试；UI 保持「已记录」态，不误显示已撤销 */
                <button
                  type="button"
                  onClick={() => void handleUndo()}
                  className="h-20 flex-1 rounded-2xl border border-warning-soft bg-warning-soft text-2xl font-semibold text-warning-ink active:bg-surface-sunken"
                >
                  撤销没有成功，再试一次
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
