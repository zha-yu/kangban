export type SubtitleSource = "partial" | "final" | "reply" | "notice";

interface BigSubtitleProps {
  /** partial=ASR 中间结果；final=ASR 终稿；reply=数字人回复；notice=系统提示。
   *  大字幕不承载信号色；说话人靠「标签 + 对齐 + 字色」三重区分 */
  text: string;
  source: SubtitleSource;
  /** 任务 / SOS 态的 UI 提示：主字幕降档，长文案不换行压到操作区 */
  compact?: boolean;
}

/** 对话态说话人：您说靠右、小伴说靠左；胶囊用品牌/中性淡底，不引入信号色 */
const SPEAKER: Record<
  "partial" | "final" | "reply",
  { label: string; badge: string; text: string; reverse: boolean }
> = {
  partial: {
    label: "正在听您说",
    badge: "bg-surface-sunken text-ink-muted",
    text: "text-ink-subtle italic",
    reverse: true,
  },
  final: {
    label: "您说",
    badge: "bg-surface-sunken text-ink-muted",
    text: "text-ink",
    reverse: true,
  },
  reply: {
    label: "小伴说",
    badge: "bg-brand-soft text-brand",
    text: "text-brand",
    reverse: false,
  },
};

/** 大字幕（FR-UI-02）：只显示当前这一句，与音频同步；完整对话看右栏记录。
 *  60px 常态引导 / 48px 对话 / 30px 任务提示，全部 ≥20px 下限，固定区内不溢出。 */
export function BigSubtitle({ text, source, compact = false }: BigSubtitleProps) {
  if (!text) return null;

  // 直接按 source 收窄，SPEAKER 才有正确的索引类型
  const speaker = !compact && source !== "notice" ? SPEAKER[source] : null;
  // 长提示（任务话术、声纹降级提示等）自动降一档，避免 60px 三行溢出固定区
  const smallNotice = !speaker && (compact || text.length > 16);

  return (
    <div className="pointer-events-none mx-auto flex h-full w-full max-w-[1600px] flex-col justify-center px-6">
      {speaker ? (
        <div
          className={`flex items-start gap-4 ${speaker.reverse ? "flex-row-reverse" : ""}`}
        >
          <span
            className={`mt-2 shrink-0 rounded-control px-3 py-1 text-xl font-semibold ${speaker.badge}`}
          >
            {speaker.label}
          </span>
          <p
            className={`line-clamp-2 min-w-0 flex-1 text-5xl font-bold leading-snug ${speaker.text} ${speaker.reverse ? "text-right" : "text-left"}`}
          >
            {text}
          </p>
        </div>
      ) : (
        <p
          className={`line-clamp-2 text-center font-bold leading-snug text-ink-muted ${smallNotice ? "text-3xl" : "text-6xl"}`}
        >
          {text}
        </p>
      )}
    </div>
  );
}
