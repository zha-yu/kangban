export type SubtitleSource = "partial" | "final" | "reply" | "notice";

interface BigSubtitleProps {
  /** partial=ASR 中间结果（灰、斜体）；final=ASR 终稿（白）；reply=数字人回复（青）；notice=系统提示（琥珀） */
  text: string;
  source: SubtitleSource;
}

const SOURCE_STYLE: Record<SubtitleSource, string> = {
  partial: "text-slate-400 italic",
  final: "text-white",
  reply: "text-cyan-200",
  notice: "text-amber-200",
};

/** 大字幕：FR-UI-02「语音回答同步显示大字幕」；正文字号 ≥20px，此处为大字号长者可读规格 */
export function BigSubtitle({ text, source }: BigSubtitleProps) {
  if (!text) return null;
  return (
    <div className="pointer-events-none mx-auto max-w-[1600px] px-16 text-center leading-snug">
      <p className={`text-6xl font-bold drop-shadow-lg ${SOURCE_STYLE[source]}`}>
        {text}
      </p>
    </div>
  );
}
