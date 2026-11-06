import { useEffect, useMemo, useState } from "react";
import type { Principal } from "../api";
import {
  ESCALATION_MS,
  MockApiError,
  STATUS_LABELS,
  SEVERITY_LABELS,
  TYPE_LABELS,
  isReopenAllowed,
  listWorkOrders,
  transitionWorkOrder,
  type Severity,
  type TransitionAction,
  type WorkOrder,
  type WorkOrderStatus,
  type WorkOrderType,
} from "../workorders-api";

/**
 * 护工工单看板（task-06 F2，console 第 3 批首项，Mock 数据）。
 *
 * 口径：FR-ALT-02 红档置顶、未处理列表即交接清单、任一工单可查完整状态变更历史；
 * FR-ALT-01 红档未确认 2 分钟升级（escalated 纯展示，升级逻辑归后端，f2 D4）；
 * PRD §4.7 状态机 + 重开仅管理员（f2 D3）。
 */

// ---------------------------------------------------------------------------
// 展示工具
// ---------------------------------------------------------------------------

const TZ = "Asia/Shanghai";

const timeFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: TZ,
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

function fmtTime(iso: string): string {
  return timeFmt.format(new Date(iso));
}

/** 红档未确认的升级倒计时文案；已超时返回 null（改用 escalated 徽标） */
function escalationCountdown(order: WorkOrder, nowMs: number): string | null {
  if (order.severity !== "red") return null;
  if (order.status !== "new") return null;
  const elapsed = nowMs - new Date(order.created_at).getTime();
  if (elapsed >= ESCALATION_MS) return null;
  const remain = Math.max(0, ESCALATION_MS - elapsed);
  const m = Math.floor(remain / 60_000);
  const s = Math.floor((remain % 60_000) / 1000);
  return `${m}:${String(s).padStart(2, "0")} 后升级`;
}

const OPEN_STATUSES: ReadonlySet<WorkOrderStatus> = new Set([
  "new",
  "acknowledged",
  "in_progress",
  "reopened",
]);

/** 徽标配色：信号色只做淡底 + 深字（前端页面方案 §1 冻结口径） */
function severityClass(severity: Severity): string {
  return severity === "red" ? "badge badge-danger" : "badge badge-warning";
}

function statusClass(status: WorkOrderStatus): string {
  switch (status) {
    case "new":
      return "badge badge-warning";
    case "acknowledged":
      return "badge badge-info";
    case "in_progress":
      return "badge badge-info";
    case "resolved":
      return "badge badge-success";
    case "closed":
      return "badge badge-neutral";
    case "reopened":
      return "badge badge-danger";
  }
}

const TYPE_OPTIONS = Object.entries(TYPE_LABELS) as [WorkOrderType, string][];
const STATUS_OPTIONS = Object.entries(STATUS_LABELS) as [WorkOrderStatus, string][];

// ---------------------------------------------------------------------------
// 操作弹窗（resolve / close / false_alarm / reopen 需要填字段）
// ---------------------------------------------------------------------------

interface ActionDialogState {
  order: WorkOrder;
  action: Extract<TransitionAction, "resolve" | "close" | "false_alarm" | "reopen">;
  title: string;
  noteLabel: string;
  needHandledAt: boolean;
}

function ActionDialog({
  state,
  error,
  busy,
  onSubmit,
  onCancel,
}: {
  state: ActionDialogState;
  error: string | null;
  busy: boolean;
  onSubmit: (note: string, actualHandledAt: string) => void;
  onCancel: () => void;
}) {
  const [note, setNote] = useState("");
  const [handledAt, setHandledAt] = useState("");

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    onSubmit(note.trim(), handledAt);
  }

  return (
    <div className="dlg-mask" role="dialog" aria-modal="true" aria-label={state.title}>
      <form className="dlg-card" onSubmit={handleSubmit}>
        <h2 className="dlg-title">{state.title}</h2>
        <p className="dlg-sub">
          {state.order.order_no} · {state.order.room_label}
        </p>

        {state.needHandledAt && (
          <label className="field">
            <span className="field-label">实际处置时间（红档必填）</span>
            <input
              type="datetime-local"
              className="field-input"
              value={handledAt}
              onChange={(e) => setHandledAt(e.target.value)}
              required
            />
          </label>
        )}

        <label className="field">
          <span className="field-label">{state.noteLabel}</span>
          <textarea
            className="field-input field-textarea"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="必填，留痕后不可改"
            rows={3}
            required
          />
        </label>

        {error && <p className="field-error">{error}</p>}

        <div className="dlg-actions">
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button type="submit" className="btn-primary" disabled={busy}>
            {busy ? "提交中…" : "确认提交"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 看板主页面
// ---------------------------------------------------------------------------

interface WorkOrdersPageProps {
  principal: Principal;
}

export function WorkOrdersPage({ principal }: WorkOrdersPageProps) {
  const [orders, setOrders] = useState<WorkOrder[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [filterStatus, setFilterStatus] = useState<WorkOrderStatus | "all" | "open">("all");
  const [filterType, setFilterType] = useState<WorkOrderType | "all">("all");
  const [filterSeverity, setFilterSeverity] = useState<Severity | "all">("all");
  const [expandedIds, setExpandedIds] = useState<ReadonlySet<string>>(new Set());
  const [dialog, setDialog] = useState<ActionDialogState | null>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const actor = useMemo(() => ({ id: principal.id, name: principal.name }), [principal]);
  const canReopen = isReopenAllowed(principal.role);

  // 升级倒计时是纯展示（f2 D4）：每秒刷新一次 nowMs 驱动重渲染
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  function reload() {
    listWorkOrders()
      .then(setOrders)
      .catch(() => setLoadError("工单加载失败，请稍后重试"));
  }

  useEffect(reload, []);

  // 排序：红档置顶（FR-ALT-02）→ 未处理在前 → 生成时间倒序
  const sorted = useMemo(() => {
    if (!orders) return null;
    const severityRank = (o: WorkOrder) => (o.severity === "red" ? 0 : 1);
    const openRank = (o: WorkOrder) => (OPEN_STATUSES.has(o.status) ? 0 : 1);
    return [...orders].sort(
      (a, b) =>
        severityRank(a) - severityRank(b) ||
        openRank(a) - openRank(b) ||
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
  }, [orders]);

  const filtered = useMemo(() => {
    if (!sorted) return null;
    return sorted.filter(
      (o) =>
        (filterStatus === "all" ||
          (filterStatus === "open" ? OPEN_STATUSES.has(o.status) : o.status === filterStatus)) &&
        (filterType === "all" || o.type === filterType) &&
        (filterSeverity === "all" || o.severity === filterSeverity),
    );
  }, [sorted, filterStatus, filterType, filterSeverity]);

  const openCount = useMemo(
    () => (orders ? orders.filter((o) => OPEN_STATUSES.has(o.status)).length : 0),
    [orders],
  );

  function openDialog(order: WorkOrder, action: ActionDialogState["action"]) {
    setDialogError(null);
    const meta: Record<ActionDialogState["action"], Pick<ActionDialogState, "title" | "noteLabel" | "needHandledAt">> = {
      resolve: {
        title: "解决工单",
        noteLabel: "处置备注（必填，护工自证留痕）",
        needHandledAt: order.severity === "red", // 红档强制实际处置时间（PRD §4.7）
      },
      close: { title: "关闭工单", noteLabel: "处置结果（关闭必填）", needHandledAt: false },
      false_alarm: { title: "标记误报并关闭", noteLabel: "误报原因（必填，保留原始输入）", needHandledAt: false },
      reopen: { title: "重开工单（管理员）", noteLabel: "重开原因（必填）", needHandledAt: false },
    };
    setDialog({ order, action, ...meta[action] });
  }

  async function submitDialog(note: string, actualHandledAt: string) {
    if (!dialog) return;
    setBusy(true);
    setDialogError(null);
    try {
      await transitionWorkOrder(dialog.order.order_id, dialog.action, { note, actual_handled_at: actualHandledAt }, actor);
      setDialog(null);
      reload();
    } catch (err) {
      setDialogError(err instanceof MockApiError ? err.message : "操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  async function quickAction(order: WorkOrder, action: Extract<TransitionAction, "acknowledge" | "start">) {
    setBusy(true);
    try {
      await transitionWorkOrder(order.order_id, action, {}, actor);
      reload();
    } catch {
      // Mock 层不会失败；真实接口接入后此处改为行内错误提示
    } finally {
      setBusy(false);
    }
  }

  /** 当前状态下可用的操作按钮（PRD §4.7 状态机；重开仅管理员，f2 D3） */
  function actionsFor(order: WorkOrder) {
    const list: React.ReactNode[] = [];
    if (order.status === "new" || order.status === "reopened") {
      list.push(
        <button key="ack" type="button" className="btn-secondary btn-sm" disabled={busy} onClick={() => quickAction(order, "acknowledge")}>
          确认
        </button>,
      );
    }
    if (order.status === "acknowledged") {
      list.push(
        <button key="start" type="button" className="btn-secondary btn-sm" disabled={busy} onClick={() => quickAction(order, "start")}>
          开始处理
        </button>,
      );
    }
    if (order.status === "in_progress") {
      list.push(
        <button key="resolve" type="button" className="btn-secondary btn-sm" disabled={busy} onClick={() => openDialog(order, "resolve")}>
          解决
        </button>,
      );
    }
    if (order.status === "resolved") {
      list.push(
        <button key="close" type="button" className="btn-secondary btn-sm" disabled={busy} onClick={() => openDialog(order, "close")}>
          关闭
        </button>,
      );
    }
    if (OPEN_STATUSES.has(order.status)) {
      list.push(
        <button key="false-alarm" type="button" className="btn-secondary btn-sm" disabled={busy} onClick={() => openDialog(order, "false_alarm")}>
          误报
        </button>,
      );
    }
    if (order.status === "closed" && canReopen) {
      list.push(
        <button key="reopen" type="button" className="btn-secondary btn-sm" disabled={busy} onClick={() => openDialog(order, "reopen")}>
          重开
        </button>,
      );
    }
    return list;
  }

  if (loadError) {
    return <div className="wo-page"><p className="wo-load-error">{loadError}</p></div>;
  }
  if (!filtered) {
    return <div className="wo-page"><p className="wo-load-hint">正在加载工单…</p></div>;
  }

  return (
    <div className="wo-page">
      {/* 筛选条：状态 / 类型 / 红黄 + 未处理视图（交接清单入口，FR-ALT-02） */}
      <div className="wo-filters" role="group" aria-label="工单筛选">
        <label className="wo-filter">
          <span>视图</span>
          <select className="field-input" value={filterStatus} onChange={(e) => setFilterStatus(e.target.value as typeof filterStatus)}>
            <option value="all">全部工单</option>
            <option value="open">未处理（交接清单）</option>
            {STATUS_OPTIONS.map(([v, label]) => (
              <option key={v} value={v}>{label}</option>
            ))}
          </select>
        </label>
        <label className="wo-filter">
          <span>类型</span>
          <select className="field-input" value={filterType} onChange={(e) => setFilterType(e.target.value as typeof filterType)}>
            <option value="all">全部类型</option>
            {TYPE_OPTIONS.map(([v, label]) => (
              <option key={v} value={v}>{label}</option>
            ))}
          </select>
        </label>
        <label className="wo-filter">
          <span>严重度</span>
          <select className="field-input" value={filterSeverity} onChange={(e) => setFilterSeverity(e.target.value as typeof filterSeverity)}>
            <option value="all">红黄全部</option>
            <option value="red">红档</option>
            <option value="yellow">黄档</option>
          </select>
        </label>
        <span className="wo-count">
          共 {filtered.length} 单 · 未处理 {openCount} 单
        </span>
      </div>

      {/* 列表：红档置顶已由排序保证 */}
      <ul className="wo-list">
        {filtered.map((o) => {
          const countdown = escalationCountdown(o, nowMs);
          const isEscalated = countdown === null && o.severity === "red" && o.status === "new";
          const expanded = expandedIds.has(o.order_id);
          return (
            <li key={o.order_id} className={`wo-item${o.severity === "red" ? " is-red" : ""}`}>
              <div className="wo-row">
                <div className="wo-badges">
                  <span className={severityClass(o.severity)}>{SEVERITY_LABELS[o.severity]}</span>
                  <span className={statusClass(o.status)}>{STATUS_LABELS[o.status]}</span>
                  {o.false_alarm && <span className="badge badge-neutral">误报</span>}
                  {isEscalated && <span className="badge badge-danger">已升级</span>}
                  {countdown && <span className="wo-countdown">{countdown}</span>}
                </div>
                <div className="wo-title">
                  <span className="wo-no">{o.order_no}</span>
                  <span className="wo-type">{TYPE_LABELS[o.type]}</span>
                  <span className="wo-elder">{o.room_label}</span>
                </div>
                <div className="wo-meta">
                  <span>建单 {fmtTime(o.created_at)}</span>
                  {o.assignee_name && <span>处理人 {o.assignee_name}</span>}
                  {o.closed_at && <span>关闭 {fmtTime(o.closed_at)}</span>}
                </div>
                <div className="wo-actions">
                  {actionsFor(o)}
                  <button
                    type="button"
                    className="btn-secondary btn-sm"
                    aria-expanded={expanded}
                    onClick={() =>
                      setExpandedIds((prev) => {
                        const next = new Set(prev);
                        if (next.has(o.order_id)) next.delete(o.order_id);
                        else next.add(o.order_id);
                        return next;
                      })
                    }
                  >
                    {expanded ? "收起详情" : "详情"}
                  </button>
                </div>
              </div>

              {expanded && (
                <div className="wo-detail">
                  <dl className="wo-snapshot">
                    <div><dt>来源类型</dt><dd>{TYPE_LABELS[o.type]}</dd></div>
                    <div><dt>建单严重度快照</dt><dd>{SEVERITY_LABELS[o.severity]}（建单时复制，不随后同步）</dd></div>
                    <div><dt>实际处置时间</dt><dd>{o.actual_handled_at ? fmtTime(o.actual_handled_at) : "—"}</dd></div>
                    <div><dt>处置备注</dt><dd>{o.note ?? "—"}</dd></div>
                    {o.false_alarm && (
                      <div><dt>误报原因（原始输入）</dt><dd>{o.false_alarm_reason ?? "—"}</dd></div>
                    )}
                  </dl>
                  {/* FR-ALT-02 验收：任一工单可查完整状态变更历史 */}
                  <h3 className="wo-history-title">状态变更历史</h3>
                  <ol className="wo-history">
                    {o.transitions.map((t) => (
                      <li key={t.transition_id} className="wo-history-item">
                        <span className="wo-history-time">{fmtTime(t.at)}</span>
                        <span className="wo-history-flow">
                          {STATUS_LABELS[t.from_status]} → {STATUS_LABELS[t.to_status]}
                        </span>
                        <span className="wo-history-actor">{t.actor_name ?? "系统"}</span>
                        {t.note && <span className="wo-history-note">{t.note}</span>}
                      </li>
                    ))}
                  </ol>
                </div>
              )}
            </li>
          );
        })}
        {filtered.length === 0 && <li className="wo-empty">当前筛选条件下没有工单</li>}
      </ul>

      {dialog && (
        <ActionDialog
          state={dialog}
          error={dialogError}
          busy={busy}
          onSubmit={submitDialog}
          onCancel={() => setDialog(null)}
        />
      )}
    </div>
  );
}
