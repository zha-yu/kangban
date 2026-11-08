import { useEffect, useRef } from "react";

/** 本次会话的一句对话。只存 final（长者）与 reply（数字人）整句，
 *  partial 中间态与 notice 系统提示仍走大字幕，不进记录。 */
export interface ChatMessage {
  id: number;
  role: "elder" | "companion";
  text: string;
}

/** 对话记录：会话中占中部右栏，可上下翻看本次会话的全部内容。
 *  长者端字阶 24px（≥20px 下限）；靠「左右对齐 + 淡底 + 说话人」区分，
 *  不用信号色、不加头像与时间戳，避免把中部变成消息流噪音。 */
export function ChatTranscript({ messages }: { messages: ChatMessage[] }) {
  const listRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);

  // 只有本来就在底部才跟随新消息；长者往上翻历史时不被强制拉回
  useEffect(() => {
    const el = listRef.current;
    if (el && atBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  return (
    <section className="flex h-full w-[960px] max-w-[60vw] shrink-0 flex-col overflow-hidden rounded-2xl border border-line bg-surface">
      <header className="flex shrink-0 items-baseline justify-between border-b border-line bg-surface-sunken px-6 py-4">
        <h2 className="text-2xl font-bold text-ink">对话记录</h2>
        <span className="text-base text-ink-muted">
          本次会话 · {messages.length} 条
        </span>
      </header>

      <div
        ref={listRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          atBottomRef.current =
            el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
        className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto p-6"
      >
        {messages.length === 0 ? (
          <p className="m-auto px-8 text-center text-2xl leading-relaxed text-ink-muted">
            您说的和小伴回的都会记在这里，可以往上翻
          </p>
        ) : (
          messages.map((message) => {
            const elder = message.role === "elder";
            return (
              <article
                key={message.id}
                className={`flex flex-col gap-2 ${elder ? "items-end" : "items-start"}`}
              >
                <span className="px-1 text-base font-semibold text-ink-subtle">
                  {elder ? "您说" : "小伴说"}
                </span>
                <p
                  className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-5 py-4 text-2xl leading-relaxed ${
                    elder
                      ? "bg-surface-sunken text-ink"
                      : "bg-brand-soft text-brand"
                  }`}
                >
                  {message.text}
                </p>
              </article>
            );
          })
        )}
      </div>
    </section>
  );
}
