/**
 * W3 声纹录入引导页（C4 契约：docs/team_tasks/day04/contracts.md §C4）。
 * 三段录音：1=念一句日常句子、2=念屏幕上的 6 位数字、3=自由说；进度 1/3 → 3/3。
 *
 * 录音交互选型：大按钮点击式（点「开始录音」→ 点「结束录音」+ 5s 自动截断），
 * 不用 HoldToTalk 的按住式——长者要看着屏幕念句子 / 数字，按住说话腾不出眼睛，
 * 点击式对本任务更贴合；段内实时计时 1–5s 有效，<1s 禁上传并提示。
 *
 * 错误处理：409 → 「这一段已录过，请重新录这一段」+ 重录本段；503 → 透出契约
 * body 话术 + 引导找照护人员 + 重录本段；422 → 可重录话术；401 → onAuthFail()。
 * 上传成功后立即释放该段音频（帧数组 / Blob 引用即弃，AC-36 原始音频不残留）。
 * 页面跳转由主流程负责：三段齐后显示完成态，经 onDone() 交还主线。
 */
import { useEffect, useRef, useState } from "react";
import { MicCaptureError } from "../audio/capture";
import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";
import {
  SEGMENT_MAX_MS,
  SEGMENT_MIN_MS,
  SegmentRecorder,
} from "./record-segment";
import { uploadSample } from "./voiceprint-client";

/** 三段引导文案（C4：句子 + 数字 + 自由说），长者可读 */
const SEGMENTS = [
  {
    index: 1 as const,
    title: "念一句话",
    hint: "请用平时说话的声音，把下面这句话念一遍：",
    sample: "今天天气真好，我想出去走一走。",
  },
  {
    index: 2 as const,
    title: "念一组数字",
    hint: "请把屏幕上的 6 位数字，一个一个慢慢念出来：",
    sample: null,
  },
  {
    index: 3 as const,
    title: "随便聊聊",
    hint: "请随便说几句话，说 3 到 5 秒就行。",
    sample: null,
  },
] as const;

/** 生成本段要念的 6 位数字（每次尝试都换一组，防止照抄上次） */
function randomDigits(): string {
  let out = "";
  for (let i = 0; i < 6; i++) out += String(Math.floor(Math.random() * 10));
  return out;
}

type Phase =
  | "idle" // 等待开始
  | "recording" // 录音中
  | "uploading" // 上传保存中
  | "too_short" // 不足 1s，禁上传
  | "duplicate" // 409 duplicate_sample
  | "failed"; // 422 / 503 / 网络（话术在 failText）

interface VoiceprintEnrollProps {
  token: string;
  /** 三段全部录入成功后的回调（跳转由主流程负责，本组件不切页） */
  onDone: () => void;
  /** 401：设备会话失效，上层回登录 */
  onAuthFail: () => void;
}

export function VoiceprintEnroll({ token, onDone, onAuthFail }: VoiceprintEnrollProps) {
  const recorderRef = useRef<SegmentRecorder | null>(null);
  const recorder = recorderRef.current ?? (recorderRef.current = new SegmentRecorder());

  const [step, setStep] = useState(0);
  const [phase, setPhase] = useState<Phase>("idle");
  const [doneCount, setDoneCount] = useState(0);
  const [finished, setFinished] = useState(false);
  const [digits, setDigits] = useState(randomDigits);
  const [failText, setFailText] = useState("");
  const [elapsedMs, setElapsedMs] = useState(0);

  const segment = SEGMENTS[step];

  // phase 镜像：自动截断回调与手动停止做幂等守卫用
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  // 段内实时计时：录音中每 100ms 读一次 recorder 计时
  useEffect(() => {
    if (phase !== "recording") return;
    const timer = setInterval(() => setElapsedMs(recorder.elapsedMs), 100);
    return () => clearInterval(timer);
  }, [phase, recorder]);

  async function handleStart(): Promise<void> {
    setFailText("");
    setElapsedMs(0);
    if (segment.index === 2) setDigits(randomDigits());
    setPhase("recording");
    try {
      await recorder.start();
    } catch (err) {
      setPhase("idle");
      setFailText(
        err instanceof MicCaptureError ? err.message : "麦克风启动失败，请稍后再试",
      );
    }
  }

  function handleStop(): void {
    if (phaseRef.current !== "recording") return; // 自动截断 / 手动停止幂等守卫
    const durationMs = recorder.stop();
    setElapsedMs(durationMs);
    if (durationMs < SEGMENT_MIN_MS) {
      recorder.clear(); // 太短不上传，音频即弃（AC-36）
      setPhase("too_short");
      setFailText("录得太短了。请对着麦克风说完再点「结束录音」，至少说 1 秒。");
      return;
    }
    void uploadCurrent();
  }

  async function uploadCurrent(): Promise<void> {
    setPhase("uploading");
    const wav = recorder.takeWav();
    recorder.clear(); // 帧数组即弃，仅保留待上传 Blob（AC-36）
    const result = await uploadSample(token, wav, segment.index);
    // 走到这里上传已结束：Blob 引用随 result 作用域结束即弃（AC-36 原始音频不残留）
    if ("kind" in result) {
      if (result.kind === "auth") {
        onAuthFail();
        return;
      }
      if (result.kind === "duplicate") {
        setPhase("duplicate");
        setFailText("这一段已录过，请重新录这一段。");
        return;
      }
      // invalid（422）/ unavailable（503 / 网络）：话术直接展示，503 不重试（C4）
      setPhase("failed");
      setFailText(result.message);
      return;
    }
    // 成功：进下一段或完成态（result.status 3 段齐时服务端应置 active）
    setDoneCount(doneCount + 1);
    setElapsedMs(0);
    if (step >= SEGMENTS.length - 1) {
      setFinished(true);
      void recorder.close(); // 三段齐：关麦清帧，麦克风不留后台
      return;
    }
    setStep(step + 1);
    setPhase("idle");
  }

  // 自动截断挂接：不设依赖数组，每次渲染重挂最新闭包，避免读到旧 state
  useEffect(() => {
    recorder.onAutoStop = handleStop;
    return () => {
      recorder.onAutoStop = null;
    };
  });

  // 卸载兜底：关麦 + 清帧（AC-36），麦克风不留后台
  useEffect(() => {
    return () => void recorder.close();
  }, [recorder]);

  const progress = (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        {SEGMENTS.map((s, i) => (
          <span
            key={s.index}
            className={`h-3 flex-1 rounded-full border ${
              finished || i < doneCount
                ? "border-brand bg-brand"
                : i === step
                  ? "border-brand-line bg-brand-soft"
                  : "border-line bg-line"
            }`}
          />
        ))}
      </div>
      <p className="text-xl text-ink-muted">已完成 {finished ? 3 : doneCount} / 3 段</p>
    </div>
  );

  if (finished) {
    return (
      <TaskShell
        eyebrow="声纹录入"
        title="录入完成，谢谢您"
        badge={
          <StatusBadge tone="success" size="md">
            3 段已保存
          </StatusBadge>
        }
        progress={progress}
        footer={
          <button
            type="button"
            onClick={onDone}
            className="h-24 w-full rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
          >
            完成
          </button>
        }
      >
        <p className="text-2xl leading-relaxed text-ink">
          我们已经记住您的声音了。以后和数字人聊天时，能更快认出您。
        </p>
        <p className="mt-4 text-xl leading-relaxed text-ink-muted">
          录音只用来提取声纹特征（加密保存），录完即弃，不会留下录音本身。
        </p>
      </TaskShell>
    );
  }

  const badge =
    phase === "recording" ? (
      <StatusBadge tone="info" size="md" pulse>
        正在录音
      </StatusBadge>
    ) : phase === "uploading" ? (
      <StatusBadge tone="info" size="md" pulse>
        正在保存…
      </StatusBadge>
    ) : phase === "idle" ? (
      <StatusBadge tone="neutral" size="md">
        等待开始
      </StatusBadge>
    ) : (
      <StatusBadge tone="warning" size="md">
        请重录本段
      </StatusBadge>
    );

  const elapsedSec = (elapsedMs / 1000).toFixed(1);
  const recording = phase === "recording";

  return (
    <TaskShell
      eyebrow={`声纹录入 · 第 ${step + 1} / 3 段`}
      title={segment.title}
      badge={badge}
      progress={progress}
    >
      <p className="text-2xl leading-relaxed text-ink-muted">{segment.hint}</p>

      {segment.index === 1 && (
        <div className="mt-8 rounded-2xl border border-line bg-surface-sunken px-8 py-10">
          <p className="text-5xl font-bold leading-snug text-ink">{segment.sample}</p>
        </div>
      )}

      {segment.index === 2 && (
        <div className="mt-8 rounded-2xl border border-line bg-surface-sunken px-8 py-10 text-center">
          <p className="text-7xl font-bold tracking-[0.35em] text-ink">{digits}</p>
        </div>
      )}

      {segment.index === 3 && (
        <p className="mt-4 text-xl leading-relaxed text-ink-muted">
          比如：「我今天早上去花园散步了」「孙子周末要来看我」。
        </p>
      )}

      {/* 段内实时计时进度条（1–5s 有效，到 5s 自动停止） */}
      {recording && (
        <div className="mt-8">
          <div className="h-4 w-full overflow-hidden rounded-full bg-line">
            <div
              className="h-full rounded-full bg-brand transition-[width] duration-100 ease-linear"
              style={{ width: `${Math.min((elapsedMs / SEGMENT_MAX_MS) * 100, 100)}%` }}
            />
          </div>
          <p className="mt-3 text-xl text-ink-muted">
            录音中 {elapsedSec} 秒（说满 1 到 {SEGMENT_MAX_MS / 1000} 秒都有效，到点自动结束）
          </p>
        </div>
      )}

      {/* 错误 / 提示区：信号色只走 StatusBadge，不铺面 */}
      {failText && !recording && (
        <div className="mt-8 rounded-2xl border border-line bg-surface-sunken px-6 py-5">
          <div className="flex items-start gap-4">
            <StatusBadge tone="warning" size="md">
              {phase === "duplicate" ? "已录过" : phase === "too_short" ? "太短了" : "没保存上"}
            </StatusBadge>
            <p className="text-2xl leading-relaxed text-ink">{failText}</p>
          </div>
          {phase === "failed" && (
            <p className="mt-3 pl-2 text-xl leading-relaxed text-ink-muted">
              请稍后在照护人员帮助下重试。
            </p>
          )}
        </div>
      )}

      <div className="mt-10">
        {recording ? (
          <button
            type="button"
            onClick={handleStop}
            className="h-24 w-full rounded-2xl bg-brand-strong text-3xl font-bold text-ink-inverse"
          >
            结束录音（已录 {elapsedSec} 秒）
          </button>
        ) : (
          <button
            type="button"
            onClick={() => void handleStart()}
            disabled={phase === "uploading"}
            className={`h-24 w-full rounded-2xl text-3xl font-bold text-ink-inverse disabled:opacity-50 ${
              phase === "idle"
                ? "bg-brand active:bg-brand-strong"
                : "bg-brand-strong"
            }`}
          >
            {phase === "uploading"
              ? "正在保存…"
              : phase === "idle"
                ? "开始录音"
                : "重录本段"}
          </button>
        )}
        <p className="mt-3 text-center text-lg text-ink-subtle">
          {recording
            ? "说完请点上面的按钮，或者等它自己结束"
            : `点一下开始，最少 ${SEGMENT_MIN_MS / 1000} 秒、最多 ${SEGMENT_MAX_MS / 1000} 秒`}
        </p>
      </div>
    </TaskShell>
  );
}
