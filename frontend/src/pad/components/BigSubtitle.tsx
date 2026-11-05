export type SubtitleSource = "partial" | "final" | "reply" | "notice";

interface BigSubtitleProps {
  /** partial=ASR 中间结果（浅灰、斜体）；final=ASR 终稿（深字）；reply=数字人回复（主色）；notice=系统提示（次级灰）
      大字幕是全页最大色块，按约定不承载信号色，避免鲜色大面积铺开 */
  text: string;
  source: SubtitleSource;
}

const SOURCE_STYLE: Record<SubtitleSource, string> = {
  partial: "text-ink-subtle italic",
  final: "text-ink",
  reply: "text-brand",
  notice: "text-ink-muted",
};

/** 大字幕：FR-UI-02「语音回答同步显示大字幕」；正文字号 ≥20px，此处为大字号长者可读规格 */
export function BigSubtitle({ text, source }: BigSubtitleProps) {
  if (!text) return null;
  return (
    <div className="pointer-events-none mx-auto max-w-[1600px] px-16 text-center leading-snug">
      <p className={`text-6xl font-bold ${SOURCE_STYLE[source]}`}>
        {text}
      </p>
    </div>
  );
}
