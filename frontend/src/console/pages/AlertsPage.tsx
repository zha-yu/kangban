import { useEffect, useMemo, useState } from "react";
import type { Principal } from "../api";
import {
  NOTIF_STATUS_FLOW,
  NOTIF_STATUS_LABELS,
  NOTIF_TYPE_LABELS,
  MockApiError,
  acknowledgeNotification,
  listNotifications,
  markNotificationRead,
  type NotificationItem,
  type NotificationStatus,
  type NotificationType,
} from "../alerts-api";

/**
 * 护工告警通知中心（task-08 F4，console，Mock 数据）。
 *
 * 口径：FR-ALT-03 + AC-23——状态机 generated→pending→delivered→acknowledged 严格顺序，
 * 确认按钮仅 delivered 可点（只有人工操作算确认）；送达≠确认、已读≠确认（read_at 与
 * acknowledged_* 并列展示）；failed 展示 retry_count；is_mock 加「演示」徽标（裁决 B5）。
 */

const TYPE_OPTIONS = Object.entries(NOTIF_TYPE_LABELS) as [NotificationType, string][];
const STATUS_OPTIONS = Object.entries(NOTIF_STATUS_LABELS) as [NotificationStatus, string][];

const TIME_FMT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

function fmtTime(iso: string): string {
  return TIME_FMT.format(new Date(iso));
}

function statusClass(status: NotificationStatus): string {
  switch (status) {
    case "generated":
      return "badge badge-neutral";
    case "pending":
      return "badge badge-warning";
    case "delivered":
      return "badge badge-info";
    case "acknowledged":
      return "badge badge-success";
    case "failed":
      return "badge badge-danger";
  }
}

/** 状态时间线（generated→pending→delivered→acknowledged；failed 单列提示） */
function StatusTimeline({ item }: { item: NotificationItem }) {
  if (item.status === "failed") {
    return (
      <p className="ntf-timeline">
        生成 {fmtTime(item.created_at)} → 重试 {item.retry_count}/3 次失败（30s 间隔）
      </p>
    );
  }
  const reached = (s: NotificationStatus) => NOTIF_STATUS_FLOW.indexOf(s) <= NOTIF_STATUS_FLOW.indexOf(item.status);
  return (
    <ol className="ntf-steps">
      {NOTIF_STATUS_FLOW.map((s) => (
        <li key={s} className={`ntf-step${reached(s) ? " is-reached" : ""}`}>
          {NOTIF_STATUS_LABELS[s]}
        </li>
      ))}
    </ol>
  );
}

export function AlertsPage({ principal }: { principal: Principal }) {
  const [items, setItems] = useState<NotificationItem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [filterStatus, setFilterStatus] = useState<NotificationStatus | "all">("all");
  const [filterType, setFilterType] = useState<NotificationType | "all">("all");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);
  const actor = useMemo(() => ({ id: principal.id, name: principal.name }), [principal]);

  function reload() {
    listNotifications()
      .then(setItems)
      .catch(() => setLoadError("通知加载失败，请稍后重试"));
  }

  useEffect(reload, []);

  // 时间倒序（同 created_at 展示口径）
  const filtered = useMemo(() => {
    if (!items) return null;
    return [...items]
      .filter(
        (n) =>
          (filterStatus === "all" || n.status === filterStatus) &&
          (filterType === "all" || n.type === filterType),
      )
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  }, [items, filterStatus, filterType]);

  /** 展开详情：标记已读（已读 ≠ 确认，AC-23） */
  function toggleExpand(n: NotificationItem) {
    setRowError(null);
    if (expandedId === n.notification_id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(n.notification_id);
    if (!n.read_at) {
      markNotificationRead(n.notification_id)
        .then((updated) => {
          setItems((prev) => prev?.map((x) => (x.notification_id === updated.notification_id ? updated : x)) ?? prev);
        })
        .catch(() => {
          // 已读标记失败不打断浏览，刷新后重试
        });
    }
  }

  async function confirm(n: NotificationItem) {
    setBusy(true);
    setRowError(null);
    try {
      const updated = await acknowledgeNotification(n.notification_id, actor);
      setItems((prev) => prev?.map((x) => (x.notification_id === updated.notification_id ? updated : x)) ?? prev);
    } catch (err) {
      setRowError(err instanceof MockApiError ? err.message : "操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  if (loadError) {
    return <div className="ntf-page"><p className="wo-load-error">{loadError}</p></div>;
  }
  if (!filtered) {
    return <div className="ntf-page"><p className="wo-load-hint">正在加载通知…</p></div>;
  }

  return (
    <div className="ntf-page">
      <p className="ntf-note">
        状态边界：送达 ≠ 确认（只有人工操作算确认），确认 ≠ 工单完成；已读只是查看留痕。
      </p>

      <div className="wo-filters" role="group" aria-label="通知筛选">
        <label className="wo-filter">
          <span>状态</span>
          <select className="field-input" value={filterStatus} onChange={(e) => setFilterStatus(e.target.value as typeof filterStatus)}>
            <option value="all">全部状态</option>
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
        <span className="wo-count">共 {filtered.length} 条</span>
      </div>

      <ul className="wo-list">
        {filtered.map((n) => {
          const expanded = expandedId === n.notification_id;
          return (
            <li key={n.notification_id} className="wo-item">
              <div className="wo-row">
                <div className="wo-badges">
                  <span className={statusClass(n.status)}>{NOTIF_STATUS_LABELS[n.status]}</span>
                  <span className="badge badge-neutral">{NOTIF_TYPE_LABELS[n.type]}</span>
                  {n.is_mock && <span className="badge badge-warning">演示</span>}
                </div>
                <div className="wo-title">
                  <span className="wo-type">{n.payload.title}</span>
                  <span className="wo-elder">{n.payload.summary}</span>
                </div>
                <div className="wo-meta">
                  <span>生成 {fmtTime(n.created_at)}</span>
                  {n.status === "failed" && <span>重试 {n.retry_count}/3 次</span>}
                  {n.acknowledged_at && <span>确认 {fmtTime(n.acknowledged_at)}</span>}
                </div>
                <div className="wo-actions">
                  {n.status === "delivered" && (
                    <button type="button" className="btn-secondary btn-sm" disabled={busy} onClick={() => confirm(n)}>
                      确认
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn-secondary btn-sm"
                    aria-expanded={expanded}
                    onClick={() => toggleExpand(n)}
                  >
                    {expanded ? "收起详情" : "详情"}
                  </button>
                </div>
              </div>

              {expanded && (
                <div className="wo-detail">
                  <StatusTimeline item={n} />
                  <dl className="wo-snapshot">
                    <div><dt>通知类型</dt><dd>{NOTIF_TYPE_LABELS[n.type]}</dd></div>
                    <div><dt>渠道</dt><dd>{n.channel === "inapp" ? "站内（保底）" : "浏览器推送"}</dd></div>
                    <div><dt>发送时间</dt><dd>{n.sent_at ? fmtTime(n.sent_at) : "未发送"}</dd></div>
                    <div><dt>已读时间</dt><dd>{n.read_at ? fmtTime(n.read_at) : "未读"}</dd></div>
                    <div><dt>确认人</dt><dd>{n.acknowledged_by_name ?? "—"}</dd></div>
                    <div><dt>确认时间</dt><dd>{n.acknowledged_at ? fmtTime(n.acknowledged_at) : "—"}</dd></div>
                    {n.dedupe_key && <div><dt>幂等键</dt><dd>{n.dedupe_key}</dd></div>}
                    {n.payload.order_no && (
                      <div><dt>关联工单</dt><dd>{n.payload.order_no}</dd></div>
                    )}
                  </dl>
                  {rowError && <p className="field-error">{rowError}</p>}
                </div>
              )}
            </li>
          );
        })}
        {filtered.length === 0 && <li className="wo-empty">当前筛选条件下没有通知</li>}
      </ul>

      {rowError && !expandedId && <p className="field-error">{rowError}</p>}
    </div>
  );
}
