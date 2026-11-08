import { useEffect } from "react";
import type { IdentityAskMsg } from "../protocol";

/**
 * C5 身份确认面板：声纹低置信/不可用时服务端发 identity_ask，长者大按钮点选。
 * 「是我」→ 回 identity_confirm（elder_id = candidates[0].elder_id）；
 * 「不是我」/ 超时不上行（契约无否认消息），后端 30s 超时自行落 generic。
 * 全屏遮罩置顶：身份未定期间打断、字幕等交互都先让位（与 SOS 结果态同级）。
 */
export function IdentityConfirm({
  ask,
  onConfirm,
  onDismiss,
}: {
  ask: IdentityAskMsg;
  onConfirm: (askId: string, elderId: string) => void;
  onDismiss: () => void;
}) {
  const elder = ask.candidates[0];

  // 前端与后端同时长收起：30s 内未确认服务端落 generic（identity_result 会再兜一层清理）
  useEffect(() => {
    const timer = setTimeout(onDismiss, Math.max(ask.timeout_s, 1) * 1000);
    return () => clearTimeout(timer);
  }, [ask.ask_id, ask.timeout_s, onDismiss]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="flex w-[880px] flex-col items-center gap-10 rounded-3xl border border-line bg-surface px-16 py-14 shadow-2xl">
        <p className="text-4xl font-semibold text-ink">
          {ask.reason === "vp_unavailable"
            ? "我没有认出您的声音，请问现在说话的是"
            : "请问现在说话的是"}
          <span className="text-brand-strong">「{elder?.display_name ?? "您"}」</span>
          吗？
        </p>
        <div className="flex items-center gap-12">
          <button
            onClick={() => elder && onConfirm(ask.ask_id, elder.elder_id)}
            className="rounded-3xl bg-brand px-16 py-8 text-5xl font-bold text-ink-inverse shadow-lg active:bg-brand-strong active:scale-95"
          >
            是我
          </button>
          <button
            onClick={onDismiss}
            className="rounded-3xl border-2 border-line bg-surface px-16 py-8 text-5xl font-bold text-ink active:scale-95"
          >
            不是我
          </button>
        </div>
        <p className="text-2xl text-ink-muted">
          请点选确认，{ask.timeout_s} 秒后自动关闭
        </p>
      </div>
    </div>
  );
}
