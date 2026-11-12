/**
 * 通用二次确认弹窗（Day 9 · C6 · S5）。
 *
 * 口径（FR-ADM-02 / AC-19）：危险操作（教学重置 / 退房 / 暂离 / 记忆删除）在弹窗确认
 * 前不发任何请求，取消则零请求；样式复用 dlg-* 弹窗族（原生 CSS + tokens 变量）。
 */

interface ConfirmDialogProps {
  title: string;
  /** 副标题：对象标识（如长者名 / 记忆摘要） */
  sub?: string;
  /** 正文说明：后果与不可逆提示 */
  body: string;
  confirmLabel: string;
  /** 危险操作：确认按钮加警示底色 */
  danger?: boolean;
  busy: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  title,
  sub,
  body,
  confirmLabel,
  danger = false,
  busy,
  error,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <div className="dlg-mask" role="dialog" aria-modal="true" aria-label={title}>
      <div className="dlg-card">
        <h2 className="dlg-title">
          {danger && <span className="badge badge-danger">危险操作</span>} {title}
        </h2>
        {sub && <p className="dlg-sub">{sub}</p>}
        <p className="dlg-sub" style={{ marginTop: 12 }}>
          {body}
        </p>

        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}

        <div className="dlg-actions">
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className={danger ? "btn-danger" : "btn-primary"}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "执行中…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
