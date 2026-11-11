import { useEffect, useState } from "react";
import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";
import { submitHealthAnswers } from "../health-client";
import { HEALTH_QUESTIONS, type HealthField } from "../tasks";

/**
 * 每日健康采集 5 问（PRD §4.6、FR-HLT-01、FR-UI-03/07）。
 *
 * 每问等 60 秒，无答 / 无效重问一次，再失败标「缺失」继续，不阻断流程；
 * 长者可主动结束。分级与写库由后端负责，这里只呈现采集过程，
 * 不判绿 / 黄 / 红、不生成业务事实。
 *
 * Day 8（day08/contracts.md §P-1）：答完自动 POST /api/pad/health-records 落库
 * （elder_id / source 服务端解出与判定）；失败不自动重试，完成页给「重新提交」
 * 按钮（重试语义按 H-2「追加新行不覆盖」如实登记，contracts.md 开放问题）。
 */
interface HealthAnswer {
  value: string;
  missing?: boolean;
}

/** 提交三态 + empty（全部缺失，无事可提交） */
type SubmitState = "idle" | "submitting" | "submitted" | "failed" | "empty";

/** 心情选项文案 → 1–5 分（与 tasks.ts HEALTH_QUESTIONS mood_score options 对齐） */
const MOOD_SCORE_BY_LABEL: Record<string, number> = {
  很差: 1,
  不太好: 2,
  一般: 3,
  比较好: 4,
  很好: 5,
};

/** "128 mmHg" / "6 小时" → 128 / 6；未答或说不出数字 → null（缺失项不传值） */
function parseNumberAnswer(raw?: string): number | null {
  if (!raw) return null;
  const match = raw.match(/(\d+(?:\.\d+)?)/);
  return match ? Number(match[1]) : null;
}

/** 界面答案 → §P-1 请求体；一项都没有（全缺失）返回 null */
function buildSubmitBody(
  answers: Partial<Record<HealthField, HealthAnswer>>,
): {
  systolic: number | null;
  diastolic: number | null;
  sleep_hours: number | null;
  mood_score: number | null;
  complaint: string | null;
} | null {
  const body = {
    systolic: parseNumberAnswer(answers.systolic?.value),
    diastolic: parseNumberAnswer(answers.diastolic?.value),
    sleep_hours: parseNumberAnswer(answers.sleep_hours?.value),
    mood_score:
      answers.mood_score && !answers.mood_score.missing
        ? (MOOD_SCORE_BY_LABEL[answers.mood_score.value] ?? null)
        : null,
    complaint:
      answers.complaint && !answers.complaint.missing && answers.complaint.value !== "没有"
        ? answers.complaint.value
        : null,
  };
  const hasAny = Object.values(body).some((v) => v !== null);
  return hasAny ? body : null;
}

interface HealthCheckupProps {
  onExit: () => void;
  onSubtitle?: (text: string) => void;
  /** 任务下发载荷里的来源标签（如「19:00 定时」）；仅展示，不参与分级 */
  sourceLabel?: string;
  /** 设备会话 access_token（§P-1 提交用）；缺失时提交态给出可读话术 */
  token?: string;
}

export function HealthCheckup({
  onExit,
  onSubtitle,
  sourceLabel,
  token,
}: HealthCheckupProps) {
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<Partial<Record<HealthField, HealthAnswer>>>(
    {},
  );
  const [remaining, setRemaining] = useState(60);
  const [retrying, setRetrying] = useState(false);
  const [numberInput, setNumberInput] = useState("");
  const [done, setDone] = useState(false);
  const [submitState, setSubmitState] = useState<SubmitState>("idle");
  const [failureMessage, setFailureMessage] = useState("");

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

  // 答完 5 问自动提交一次（§P-1）；失败不自动重试，由完成页按钮触发
  useEffect(() => {
    if (!done || submitState !== "idle") return;
    void doSubmit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [done]);

  async function doSubmit(): Promise<void> {
    const body = buildSubmitBody(answers);
    if (!body) {
      setSubmitState("empty");
      return;
    }
    if (!token) {
      setSubmitState("failed");
      setFailureMessage("登录已过期，请联系工作人员");
      return;
    }
    setSubmitState("submitting");
    const result = await submitHealthAnswers(token, body);
    if ("severity" in result) {
      setSubmitState("submitted");
      return;
    }
    setSubmitState("failed");
    setFailureMessage(
      result.kind === "device_unbound"
        ? "设备还没绑定到床位，请联系工作人员"
        : result.kind === "auth"
          ? "登录已过期，请联系工作人员"
          : result.message,
    );
  }

  /** 中途退出且已有答案：先落库再回主界面（不阻塞跳转，结果不回写本组件状态） */
  function handleExit(): void {
    const body = buildSubmitBody(answers);
    if (body && token) {
      void submitHealthAnswers(token, body);
    }
    onExit();
  }

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
    // 小数仅 sleep_hours（question.decimal）；血压保持整数（§P-1 值域照 H-2）
    const valid = question.decimal
      ? /^\d+(\.\d+)?$/.test(value)
      : /^\d+$/.test(value);
    if (!valid) return;
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
          submitState === "submitted" ? (
            <StatusBadge tone="info" size="md">
              已提交 · 等待工作人员复核
            </StatusBadge>
          ) : submitState === "submitting" ? (
            <StatusBadge tone="info" size="md" pulse>
              正在提交…
            </StatusBadge>
          ) : submitState === "failed" ? (
            <StatusBadge tone="warning" size="md">
              提交失败：{failureMessage}
            </StatusBadge>
          ) : (
            <StatusBadge tone="neutral" size="md">
              本次未采集到数据
            </StatusBadge>
          )
        }
        progress={progress}
        footer={
          submitState === "failed" ? (
            <button
              type="button"
              onClick={() => void doSubmit()}
              className="h-24 w-full rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
            >
              重新提交
            </button>
          ) : (
            <button
              type="button"
              onClick={onExit}
              className="h-24 w-full rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
            >
              返回主界面
            </button>
          )
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
      onExit={handleExit}
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
              onChange={(e) => {
                const raw = e.target.value.replace(
                  question.decimal ? /[^\d.]/g : /\D/g,
                  "",
                );
                if (!question.decimal) {
                  setNumberInput(raw);
                  return;
                }
                // 只保留第一个小数点：后续点号丢弃（"6.5.5" → "6.55"，"6." 合法过渡态）
                const [intPart, ...rest] = raw.split(".");
                setNumberInput(
                  rest.length > 0 ? `${intPart}.${rest.join("")}` : intPart,
                );
              }}
              inputMode={question.decimal ? "decimal" : "numeric"}
              placeholder={question.decimal ? "例如 6.5" : "例如 128"}
              className="h-20 w-72 rounded-2xl border border-line bg-surface px-6 text-4xl text-ink outline-none placeholder:text-ink-subtle focus:ring-2 focus:ring-brand"
            />
          </label>
          <button
            type="button"
            onClick={handleNumber}
            disabled={
              !(
                question.decimal
                  ? /^\d+(\.\d+)?$/
                  : /^\d+$/
              ).test(numberInput.trim())
            }
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
