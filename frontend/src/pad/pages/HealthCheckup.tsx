import { useEffect, useState } from "react";
import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";
import { HEALTH_QUESTIONS, type HealthField } from "../tasks";

/**
 * 每日健康采集 5 问（PRD §4.6、FR-HLT-01、FR-UI-03/07）。
 *
 * 每问等 60 秒，无答 / 无效重问一次，再失败标「缺失」继续，不阻断流程；
 * 长者可主动结束。分级与写库由后端负责，这里只呈现采集过程，
 * 不判绿 / 黄 / 红、不生成业务事实。
 */
interface HealthAnswer {
  value: string;
  missing?: boolean;
}

interface HealthCheckupProps {
  onExit: () => void;
  onSubtitle?: (text: string) => void;
  /** 任务下发载荷里的来源标签（如「19:00 定时」）；仅展示，不参与分级 */
  sourceLabel?: string;
}

export function HealthCheckup({
  onExit,
  onSubtitle,
  sourceLabel,
}: HealthCheckupProps) {
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<Partial<Record<HealthField, HealthAnswer>>>(
    {},
  );
  const [remaining, setRemaining] = useState(60);
  const [retrying, setRetrying] = useState(false);
  const [numberInput, setNumberInput] = useState("");
  const [done, setDone] = useState(false);

  const question = HEALTH_QUESTIONS[Math.min(index, HEALTH_QUESTIONS.length - 1)];

  // 每问独立 60 秒窗口；进入下一问时重置
  useEffect(() => {
    if (done) return;
    setRemaining(60);
    const timer = setInterval(
      () => setRemaining((v) => Math.max(0, v - 1)),
      1000,
    );
    return () => clearInterval(timer);
  }, [index, done]);

  function advance(answer: HealthAnswer) {
    const next = { ...answers, [question.key]: answer };
    setAnswers(next);
    setRetrying(false);
    setNumberInput("");
    onSubtitle?.(answer.missing ? "好的，这一项先记为未采集" : "好的，记下了");
    if (index >= HEALTH_QUESTIONS.length - 1) {
      setDone(true);
      return;
    }
    setIndex((i) => i + 1);
  }

  function handleNumber() {
    const value = numberInput.trim();
    if (!/^\d+$/.test(value)) return;
    advance({ value: `${value}${question.unit ? ` ${question.unit}` : ""}` });
  }

  function handleRetry() {
    setRetrying(true);
    setRemaining(60);
    onSubtitle?.("没听清，请您再说一次");
  }

  const progress = (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        {HEALTH_QUESTIONS.map((q, i) => (
          <span
            key={q.key}
            className={`h-3 flex-1 rounded-full border ${
              done || i < index
                ? "border-brand bg-brand"
                : i === index
                  ? "border-brand-line bg-brand-soft"
                  : "border-line bg-line"
            }`}
          />
        ))}
      </div>
      <p className="text-xl text-ink-muted">
        已完成 {done ? 5 : index} / 5 问
      </p>
    </div>
  );

  if (done) {
    return (
      <TaskShell
        eyebrow={`每日健康采集${sourceLabel ? ` · ${sourceLabel}` : ""}`}
        title="今天问完了，谢谢您"
        badge={
          <StatusBadge tone="info" size="md">
            已提交 · 等待工作人员复核
          </StatusBadge>
        }
        progress={progress}
        footer={
          <button
            type="button"
            onClick={onExit}
            className="h-24 w-full rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
          >
            返回主界面
          </button>
        }
      >
        <div className="grid grid-cols-2 gap-4">
          {HEALTH_QUESTIONS.map((q, i) => {
            const answer = answers[q.key];
            return (
              <div
                key={q.key}
                className="flex items-center justify-between gap-4 rounded-2xl border border-line bg-surface-sunken px-6 py-5"
              >
                <span className="text-xl text-ink-muted">
                  {i + 1}. {q.label.replace("？", "")}
                </span>
                {answer?.missing || !answer ? (
                  <StatusBadge tone="neutral" size="sm">
                    未采集
                  </StatusBadge>
                ) : (
                  <span className="text-2xl font-semibold text-ink">
                    {answer.value}
                  </span>
                )}
              </div>
            );
          })}
        </div>
        <p className="mt-6 text-xl text-ink-muted">
          本页为教学模拟信息，不构成医疗诊断；分级结果由工作人员复核后录入。
        </p>
      </TaskShell>
    );
  }

  return (
    <TaskShell
      eyebrow={`每日健康采集 · 第 ${index + 1} / 5 问${sourceLabel ? ` · ${sourceLabel}` : ""}`}
      title={question.label}
      badge={
        <StatusBadge tone={retrying ? "warning" : "info"} size="md" pulse={!retrying}>
          {retrying ? "再问一次" : "正在听"}
        </StatusBadge>
      }
      onExit={onExit}
      exitLabel="结束采集"
      progress={progress}
      footer={
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={handleRetry}
            className="h-20 rounded-2xl border border-line bg-surface px-8 text-2xl font-semibold text-ink active:bg-surface-sunken"
          >
            没听清，再问一次
          </button>
          <button
            type="button"
            onClick={() => advance({ value: "", missing: true })}
            className="h-20 rounded-2xl border border-line bg-surface px-8 text-2xl font-semibold text-ink-muted active:bg-surface-sunken"
          >
            {retrying ? "仍答不上来，标缺失" : "这题先跳过"}
          </button>
          <span className="ml-auto text-2xl text-ink-muted">
            等待回答 {remaining} 秒
          </span>
        </div>
      }
    >
      <p className="text-2xl text-ink-muted">{question.hint}</p>

      {/* 文字按钮兜底：麦克风权限被拒时仍能完成采集（FR-UI-03） */}
      {question.kind === "number" ? (
        <div className="mt-8 flex items-end gap-4">
          <label className="flex flex-col gap-3">
            <span className="text-xl text-ink-muted">也可以直接输入数字</span>
            <input
              value={numberInput}
              onChange={(e) => setNumberInput(e.target.value.replace(/\D/g, ""))}
              inputMode="numeric"
              placeholder="例如 128"
              className="h-20 w-72 rounded-2xl border border-line bg-surface px-6 text-4xl text-ink outline-none placeholder:text-ink-subtle focus:ring-2 focus:ring-brand"
            />
          </label>
          <button
            type="button"
            onClick={handleNumber}
            disabled={!/^\d+$/.test(numberInput.trim())}
            className="h-20 rounded-2xl bg-brand px-12 text-3xl font-bold text-ink-inverse active:bg-brand-strong disabled:opacity-50"
          >
            确认
          </button>
          {question.unit && (
            <span className="pb-5 text-2xl text-ink-muted">{question.unit}</span>
          )}
        </div>
      ) : (
        <div className="mt-8 grid grid-cols-3 gap-4">
          {question.options?.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() =>
                advance({
                  value: `${option.label}${question.unit ? ` ${question.unit}` : ""}`,
                })
              }
              className="h-20 rounded-2xl border border-line bg-surface text-3xl font-semibold text-ink active:bg-surface-sunken"
            >
              {option.label}
            </button>
          ))}
        </div>
      )}

      {/* 已答摘要：缺失项用中性徽标，不靠颜色判等级 */}
      {index > 0 && (
        <div className="mt-8 flex flex-wrap items-center gap-3 border-t border-line pt-6">
          <span className="text-xl text-ink-muted">已记录</span>
          {HEALTH_QUESTIONS.slice(0, index).map((q, i) => (
            <span
              key={q.key}
              className="rounded-xl border border-line bg-surface-sunken px-4 py-2 text-xl text-ink"
            >
              {i + 1}. {answers[q.key]?.missing ? "未采集" : answers[q.key]?.value}
            </span>
          ))}
        </div>
      )}
    </TaskShell>
  );
}
