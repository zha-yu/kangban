/**
 * 管理员 · 记忆管理（Day 9 · C6 · S5，节点 C6 三页之一）。
 *
 * 覆盖（任务卡 §6.2）：列表 / 纠正 / 删除，消费 app/api/memory.py（X5 冻结契约）。
 * 纠正走弹窗改文（旧值 superseded、新值生效，FR-MEM-03/06）；删除走二次确认弹窗，
 * 确认后调用，成功后**列表即时移除**（再查不再召回，FR-MEM-05/08，TD:1172）。
 *
 * 骨架先行：X5（B2 memory_admin_service）未交付时后端 503 → 页面「X5 未交付」横幅。
 * 长者隔离与越权审计在 service 层（403 文案不静默，F25 同口径）。
 */

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api";
import {
  correctMemory,
  deleteMemory,
  listMemories,
  MemoryApiError,
  memoryItemText,
  type MemoryItem,
} from "../memory-api";
import { listElders, type ElderListItem } from "../elders-api";
import { ConfirmDialog } from "./ConfirmDialog";

function errorText(err: unknown): string {
  if (err instanceof MemoryApiError || err instanceof ApiError) return err.message;
  return "操作失败，请稍后重试";
}

/** P2-25 同款：page_size=100 循环取满 total */
const ELDERS_PAGE_SIZE = 100;
async function fetchAllElders(): Promise<ElderListItem[]> {
  const first = await listElders({ page: 1, pageSize: ELDERS_PAGE_SIZE });
  const items = [...first.items];
  const totalPages = Math.ceil(first.total / ELDERS_PAGE_SIZE);
  for (let page = 2; page <= totalPages; page++) {
    const res = await listElders({ page, pageSize: ELDERS_PAGE_SIZE });
    items.push(...res.items);
  }
  return items;
}

function elderLabel(e: ElderListItem): string {
  return e.room_no ? `${e.name}（${e.room_no} ${e.bed_no ?? ""}）` : `${e.name}（未入床）`;
}

const timeFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

function fmtTime(iso: string | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : timeFmt.format(d);
}

export function AdminMemoryPage() {
  const [elders, setElders] = useState<ElderListItem[] | null>(null);
  const [eldersError, setEldersError] = useState<string | null>(null);
  const [elderId, setElderId] = useState("");

  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // 纠正弹窗
  const [correcting, setCorrecting] = useState<MemoryItem | null>(null);
  const [correctText, setCorrectText] = useState("");
  const [correctBusy, setCorrectBusy] = useState(false);
  const [correctError, setCorrectError] = useState<string | null>(null);

  // 删除确认弹窗
  const [deleting, setDeleting] = useState<MemoryItem | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    fetchAllElders()
      .then(setElders)
      .catch(() => setEldersError("长者列表加载失败，请稍后重试"));
  }, []);

  const loadMemories = useCallback((id: string) => {
    if (!id) {
      setItems(null);
      return;
    }
    setItems(null);
    setLoadError(null);
    listMemories(id)
      .then((res) => setItems(res.items))
      .catch((err: unknown) => setLoadError(errorText(err)));
  }, []);

  useEffect(() => {
    loadMemories(elderId);
    setNotice(null);
  }, [elderId, loadMemories]);

  const notDelivered = loadError?.includes("X5 未交付") ?? false;

  async function submitCorrect() {
    if (!correcting || !elderId) return;
    const newText = correctText.trim();
    if (!newText) {
      setCorrectError("请输入纠正后的内容");
      return;
    }
    setCorrectBusy(true);
    setCorrectError(null);
    try {
      await correctMemory(elderId, correcting.chunk_id, newText);
      setCorrecting(null);
      setNotice("记忆已纠正：旧值标记 superseded，新值生效");
      loadMemories(elderId);
    } catch (err) {
      setCorrectError(errorText(err));
    } finally {
      setCorrectBusy(false);
    }
  }

  async function submitDelete() {
    if (!deleting || !elderId) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteMemory(elderId, deleting.chunk_id);
      // 删除后列表即时移除（任务卡 §6.4：再查不再召回）
      setItems((prev) => prev?.filter((m) => m.chunk_id !== deleting.chunk_id) ?? prev);
      setDeleting(null);
      setNotice("记忆已删除，立即不再被召回");
    } catch (err) {
      setDeleteError(errorText(err));
    } finally {
      setDeleteBusy(false);
    }
  }

  if (eldersError) {
    return (
      <div className="adm-page">
        <p className="wo-load-error">{eldersError}</p>
      </div>
    );
  }
  if (!elders) {
    return (
      <div className="adm-page">
        <p className="wo-load-hint">正在加载长者列表…</p>
      </div>
    );
  }

  return (
    <div className="adm-page">
      <div className="adm-toolbar">
        <label className="field">
          <span className="field-label">选择长者</span>
          <select
            className="field-input"
            value={elderId}
            onChange={(e) => setElderId(e.target.value)}
          >
            <option value="">请选择…</option>
            {elders.map((e) => (
              <option key={e.elder_id} value={e.elder_id}>
                {elderLabel(e)}
              </option>
            ))}
          </select>
        </label>
        {items && <span className="wo-count">共 {items.length} 条长期记忆</span>}
      </div>

      {/* X5 未交付横幅（骨架先行口径） */}
      {notDelivered && (
        <p className="alert-danger" role="status">
          X5 未交付：记忆管理服务尚未部署，当前仅页面骨架可浏览。待 B2 交付后此处自动可用。
        </p>
      )}
      {loadError && !notDelivered && <p className="alert-danger" role="alert">{loadError}</p>}
      {notice && <p className="adm-result" role="status">{notice}</p>}

      {!elderId && !loadError && (
        <p className="wo-load-hint">选择一位长者查看其长期记忆（查看 / 纠正 / 删除）。</p>
      )}

      <ul className="mem-list">
        {(items ?? []).map((m) => (
          <li key={m.chunk_id} className="mem-item">
            <div className="mem-text">{memoryItemText(m)}</div>
            <div className="mem-meta">
              {m.status && <span className="badge badge-neutral">{m.status}</span>}
              <span>{fmtTime(m.created_at)}</span>
              <span className="mem-id">{m.chunk_id}</span>
            </div>
            <div className="wo-actions">
              <button
                type="button"
                className="btn-secondary btn-sm"
                onClick={() => {
                  setCorrecting(m);
                  setCorrectText(memoryItemText(m));
                  setCorrectError(null);
                  setNotice(null);
                }}
              >
                纠正
              </button>
              <button
                type="button"
                className="btn-secondary btn-sm"
                onClick={() => {
                  setDeleting(m);
                  setDeleteError(null);
                  setNotice(null);
                }}
              >
                删除
              </button>
            </div>
          </li>
        ))}
        {items && items.length === 0 && (
          <li className="wo-empty">该长者暂无长期记忆</li>
        )}
      </ul>

      {correcting && (
        <div className="dlg-mask" role="dialog" aria-modal="true" aria-label="纠正记忆">
          <form
            className="dlg-card"
            onSubmit={(e) => {
              e.preventDefault();
              void submitCorrect();
            }}
          >
            <h2 className="dlg-title">纠正记忆</h2>
            <p className="dlg-sub">旧值将标记 superseded 并保留留痕，新值生效（FR-MEM-03/06）</p>
            <label className="field">
              <span className="field-label">纠正后的内容</span>
              <textarea
                className="field-input field-textarea"
                value={correctText}
                onChange={(e) => setCorrectText(e.target.value)}
                rows={3}
                required
              />
            </label>
            {correctError && <p className="field-error" role="alert">{correctError}</p>}
            <div className="dlg-actions">
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setCorrecting(null)}
                disabled={correctBusy}
              >
                取消
              </button>
              <button type="submit" className="btn-primary" disabled={correctBusy}>
                {correctBusy ? "提交中…" : "确认纠正"}
              </button>
            </div>
          </form>
        </div>
      )}

      {deleting && (
        <ConfirmDialog
          title="删除记忆"
          sub={memoryItemText(deleting)}
          body="删除后该条记忆立即不再被召回（FR-MEM-05/08），操作不可撤销。确定继续吗？"
          confirmLabel="确认删除"
          danger
          busy={deleteBusy}
          error={deleteError}
          onConfirm={() => void submitDelete()}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
