/**
 * 管理员 · 统计与审计（Day 9 · C6 · S5，节点 C6 三页之一）。
 *
 * 覆盖（任务卡 §6.2）：FR-ALT-05 三项统计指标（消费 app/api/stats.py，已交付 F13）＋
 * 审计日志表 + 导出 CSV 按钮（消费 A4，路径集中在 stats-api `AUDIT_ENDPOINTS`，
 * A4 未交付时 404 → 页面「A4 未交付」横幅，统计区不受影响）。
 *
 * 审计筛选口径（A4 计划草案）：按时间（起止日期）与操作人；A4 定稿后只改 stats-api。
 * 原生 CSS + var() tokens，不引组件库。
 */

import { useEffect, useState } from "react";
import { ApiError } from "../api";
import {
  auditCell,
  AuditApiError,
  exportAuditCsv,
  formatAuditTime,
  getWeeklyStats,
  listAuditLogs,
  type AuditLogFilter,
  type AuditLogItem,
  type WeeklyStats,
} from "../stats-api";

function errorText(err: unknown): string {
  if (err instanceof AuditApiError || err instanceof ApiError) return err.message;
  return "操作失败，请稍后重试";
}

/** 周起始（UTC ISO）→ 上海自然日展示 */
const dayFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

export function AdminStatsPage() {
  // ---- 统计（F13 已交付） ----
  const [stats, setStats] = useState<WeeklyStats | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);

  useEffect(() => {
    getWeeklyStats()
      .then(setStats)
      .catch((err: unknown) => setStatsError(errorText(err)));
  }, []);

  // ---- 审计日志（A4 骨架先行） ----
  const [filter, setFilter] = useState<AuditLogFilter>({});
  const [auditItems, setAuditItems] = useState<AuditLogItem[] | null>(null);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [auditBusy, setAuditBusy] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);

  async function loadAudit(f: AuditLogFilter) {
    setAuditBusy(true);
    setAuditError(null);
    try {
      const res = await listAuditLogs(f);
      setAuditItems(res.items);
    } catch (err) {
      setAuditItems(null);
      setAuditError(errorText(err));
    } finally {
      setAuditBusy(false);
    }
  }

  useEffect(() => {
    void loadAudit({});
  }, []);

  async function handleExport() {
    setExportBusy(true);
    try {
      await exportAuditCsv(filter);
    } catch (err) {
      setAuditError(errorText(err));
    } finally {
      setExportBusy(false);
    }
  }

  const auditNotDelivered = auditError?.includes("A4 未交付") ?? false;

  return (
    <div className="adm-page">
      {/* ---- 本周运营统计（FR-ALT-05 三指标） ---- */}
      <h2 className="adm-section-title">本周运营统计</h2>
      {statsError && <p className="alert-danger" role="alert">{statsError}</p>}
      {stats && (
        <>
          <p className="adm-note">
            统计周期自 {dayFmt.format(new Date(stats.week_start))}（周一）起（Asia/Shanghai 自然周）
          </p>
          <div className="adm-cards">
            <div className="adm-card">
              <div className="adm-card-value">{stats.red_events_count}</div>
              <div className="adm-card-label">本周红档事件数</div>
            </div>
            <div className="adm-card">
              <div className="adm-card-value">
                {stats.avg_ack_minutes === null ? "—" : stats.avg_ack_minutes}
              </div>
              <div className="adm-card-label">平均确认时长（分钟）</div>
            </div>
            <div className="adm-card">
              <div className="adm-card-value">
                {Object.values(stats.missed_by_building).reduce((a, b) => a + b, 0)}
              </div>
              <div className="adm-card-label">本周漏采工单总数</div>
            </div>
          </div>
          <section className="adm-section">
            <h3 className="adm-section-title">各楼栋漏采次数</h3>
            {Object.keys(stats.missed_by_building).length === 0 ? (
              <p className="adm-note">本周各楼栋均无漏采。</p>
            ) : (
              <ul className="mem-list">
                {Object.entries(stats.missed_by_building).map(([building, count]) => (
                  <li key={building} className="mem-item">
                    <div className="mem-text">{building} 楼栋</div>
                    <div className="wo-count">{count} 次</div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}

      {/* ---- 审计日志（A4 骨架先行） ---- */}
      <h2 className="adm-section-title">审计日志</h2>
      <div className="adm-toolbar">
        <label className="field">
          <span className="field-label">开始日期</span>
          <input
            type="date"
            className="field-input"
            value={filter.start_date ?? ""}
            onChange={(e) => setFilter((f) => ({ ...f, start_date: e.target.value || undefined }))}
          />
        </label>
        <label className="field">
          <span className="field-label">结束日期</span>
          <input
            type="date"
            className="field-input"
            value={filter.end_date ?? ""}
            onChange={(e) => setFilter((f) => ({ ...f, end_date: e.target.value || undefined }))}
          />
        </label>
        <label className="field">
          <span className="field-label">操作人</span>
          <input
            type="text"
            className="field-input"
            placeholder="姓名或 ID"
            value={filter.actor ?? ""}
            onChange={(e) => setFilter((f) => ({ ...f, actor: e.target.value || undefined }))}
          />
        </label>
        <button
          type="button"
          className="btn-secondary"
          disabled={auditBusy}
          onClick={() => void loadAudit(filter)}
        >
          {auditBusy ? "查询中…" : "查询"}
        </button>
        <button type="button" className="btn-primary" disabled={exportBusy || auditBusy} onClick={() => void handleExport()}>
          {exportBusy ? "导出中…" : "导出 CSV"}
        </button>
      </div>

      {auditNotDelivered && (
        <p className="alert-danger" role="status">
          A4 未交付：审计端点尚未部署，当前仅页面骨架可浏览。待 dev-A 交付后此处自动可用（路径已在
          stats-api 集中，对齐只需改一处）。
        </p>
      )}
      {auditError && !auditNotDelivered && <p className="alert-danger" role="alert">{auditError}</p>}

      {auditItems && (
        <div className="adm-table-wrap">
          <table className="adm-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>操作人</th>
                <th>详情</th>
                <th>trace_id</th>
              </tr>
            </thead>
            <tbody>
              {auditItems.map((it, idx) => (
                <tr key={it.id ?? idx}>
                  <td>{formatAuditTime(it.created_at)}</td>
                  <td>{auditCell(it.action)}</td>
                  <td>{auditCell(it.actor_name ?? it.actor_id)}</td>
                  <td>{auditCell(it.detail)}</td>
                  <td className="mem-id">{auditCell(it.trace_id)}</td>
                </tr>
              ))}
              {auditItems.length === 0 && (
                <tr>
                  <td colSpan={5} style={{ textAlign: "center", color: "var(--color-ink-muted)" }}>
                    当前筛选条件下没有审计记录
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
