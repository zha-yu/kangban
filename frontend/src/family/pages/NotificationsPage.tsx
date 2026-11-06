import { useEffect, useState } from "react";
import {
  ApiError,
  fetchNotifications,
  markNotificationRead,
  NETWORK_ERROR_TEXT,
  type FamilyNotification,
} from "../api";

/**
 * 通知中心（契约 N1/N4；FR-ALT-01、FR-RPT-03）。
 * 点击卡片标记已读（幂等）；payload.report_id 存在时可跳转日报详情（S-02）。
 * 模拟推送状态按 PRD 390 渲染「模拟推送 · 已送达 / 重试 N 次」，不显示真实发送口径。
 */
interface NotificationsPageProps {
  onOpenReport: (reportId: string) => void;
}

/** 通知类型 → 徽标 tone 与中文标签（TD 606 开放枚举，未知类型回退 neutral） */
const TYPE_META: Record<string, { label: string; tone: "danger" | "warning" | "info" | "neutral" }> = {
  emergency_health: { label: "健康急情", tone: "danger" },
  sos: { label: "一键求助", tone: "danger" },
  sensitive: { label: "敏感事件", tone: "warning" },
  missed_medication: { label: "用药提醒", tone: "warning" },
  report_ready: { label: "日报生成", tone: "info" },
};

export function NotificationsPage({ onOpenReport }: NotificationsPageProps) {
  const [items, setItems] = useState<FamilyNotification[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchNotifications()
      .then((data) => {
        if (!cancelled) setItems(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : NETWORK_ERROR_TEXT);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleRead(n: FamilyNotification) {
    if (n.read_at) return;
    try {
      const readAt = await markNotificationRead(n.notification_id);
      setItems(
        (prev) => prev?.map((it) => (it.notification_id === n.notification_id ? { ...it, read_at: readAt } : it)) ?? prev,
      );
    } catch {
      // 已读失败不打断浏览（幂等接口，重进页面可再试）
    }
  }

  if (error) return <p className="empty-state">{error}</p>;
  if (!items) {
    return (
      <div className="loading-row" role="status" aria-label="正在加载通知">
        <span className="spinner" />
      </div>
    );
  }

  const unreadCount = items.filter((n) => !n.read_at).length;

  return (
    <section aria-label="通知中心">
      <p className="section-note">
        共 {items.length} 条{unreadCount > 0 ? ` · ${unreadCount} 条未读` : ""}
      </p>
      {items.length === 0 && <p className="empty-state">暂无通知</p>}
      <ul className="card-list">
        {items.map((n) => {
          const meta = TYPE_META[n.type] ?? { label: "通知", tone: "neutral" as const };
          return (
            <li key={n.notification_id}>
              <button
                type="button"
                className={`notif-card${n.read_at ? "" : " notif-unread"}`}
                onClick={() => handleRead(n)}
              >
                <span className="notif-head">
                  {!n.read_at && <span className="unread-dot" aria-label="未读" />}
                  <span className={`badge tone-${meta.tone}`}>{meta.label}</span>
                  {n.is_mock && <span className="badge tone-neutral">模拟推送</span>}
                  {n.status === "failed" && <span className="badge tone-danger">推送失败</span>}
                  {n.retry_count > 0 && n.status !== "failed" && (
                    <span className="badge tone-warning">重试 {n.retry_count} 次</span>
                  )}
                </span>
                <span className="notif-brief">
                  {n.payload.elder_name} · {n.payload.event_brief}
                </span>
                <span className="notif-meta">
                  {formatTime(n.created_at)}
                  {n.payload.report_id && (
                    <span
                      className="notif-link"
                      role="link"
                      tabIndex={0}
                      onClick={(e) => {
                        e.stopPropagation();
                        onOpenReport(n.payload.report_id!);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.stopPropagation();
                          onOpenReport(n.payload.report_id!);
                        }
                      }}
                    >
                      查看日报
                    </span>
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
