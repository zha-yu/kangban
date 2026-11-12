import { useEffect, useState } from "react";
import {
  ApiError,
  fetchReport,
  fetchReports,
  NETWORK_ERROR_TEXT,
  type DailyReportDetail,
  type DailyReportSummary,
  type MedicationStats,
} from "../api";

/**
 * 健康日报：列表 + 详情（契约 F1/F2；FR-RPT-01/02）。
 * 列表同日期只出最新版（S-07）；详情含用药五类分列（S-01 Day9 v1.3 第五键
 * late_taken_reported=窗口外补报，不合并「实际漏服」）、健康摘要与需关注事件
 * （S-09 服务端组装）。audio_url 非空渲染语音条播放器、null 不渲染（FR-RPT-04，
 * Day 9 F30：C4 生成侧联调 D11 14:00，前端按冻结字段先行）。
 */
interface ReportsPageProps {
  elderId: string;
  elderName: string;
  openReportId: string | null;
  onOpenReport: (reportId: string) => void;
  onCloseReport: () => void;
}

const MED_LABELS: Array<{ key: keyof MedicationStats; label: string; tone: Tone }> = [
  { key: "taken_reported", label: "已服自报", tone: "success" },
  { key: "refused", label: "拒服", tone: "warning" },
  { key: "missed_unconfirmed", label: "已送达未确认", tone: "info" },
  { key: "notification_failed", label: "通知失败", tone: "danger" },
  { key: "late_taken_reported", label: "窗口外补报", tone: "info" },
];

type Tone = "success" | "warning" | "info" | "danger";

export function ReportsPage({
  elderId,
  elderName,
  openReportId,
  onOpenReport,
  onCloseReport,
}: ReportsPageProps) {
  const [reports, setReports] = useState<DailyReportSummary[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [detail, setDetail] = useState<DailyReportDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setReports(null);
    setListError(null);
    fetchReports(elderId)
      .then((items) => {
        if (!cancelled) setReports(items);
      })
      .catch((err: unknown) => {
        if (!cancelled) setListError(err instanceof ApiError ? err.message : NETWORK_ERROR_TEXT);
      });
    return () => {
      cancelled = true;
    };
  }, [elderId]);

  useEffect(() => {
    if (!openReportId) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    let cancelled = false;
    setDetail(null);
    setDetailError(null);
    fetchReport(openReportId)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((err: unknown) => {
        if (!cancelled) setDetailError(err instanceof ApiError ? err.message : NETWORK_ERROR_TEXT);
      });
    return () => {
      cancelled = true;
    };
  }, [openReportId]);

  if (openReportId) {
    return (
      <section aria-label="日报详情">
        <button type="button" className="back-btn" onClick={onCloseReport}>
          ← 返回列表
        </button>
        {detailError && <p className="empty-state">{detailError}</p>}
        {!detailError && !detail && (
          <div className="loading-row" role="status" aria-label="正在加载日报详情">
            <span className="spinner" />
          </div>
        )}
        {detail && <ReportDetail report={detail} elderName={elderName} />}
      </section>
    );
  }

  if (listError) return <p className="empty-state">{listError}</p>;
  if (!reports) {
    return (
      <div className="loading-row" role="status" aria-label="正在加载日报列表">
        <span className="spinner" />
      </div>
    );
  }
  if (reports.length === 0) {
    return <p className="empty-state">暂无日报，生成后将在此展示</p>;
  }

  return (
    <section aria-label="日报列表">
      <ul className="card-list">
        {reports.map((r) => (
          <li key={r.report_id}>
            <button type="button" className="report-card" onClick={() => onOpenReport(r.report_id)}>
              <span className="report-card-head">
                <span className="report-date">{r.report_date}</span>
                {r.version > 1 && <span className="badge tone-info">已更新 · v{r.version}</span>}
                {r.status === "away_notice" && <span className="badge tone-neutral">暂离告知</span>}
              </span>
              <span className="report-preview">{r.summary_preview}…</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function ReportDetail({ report, elderName }: { report: DailyReportDetail; elderName: string }) {
  return (
    <article className="detail-card">
      <h2 className="detail-title">
        {report.report_date} · {elderName}
        {report.version > 1 && <span className="badge tone-info">v{report.version}</span>}
        {report.status === "away_notice" && <span className="badge tone-neutral">暂离告知</span>}
      </h2>

      {report.attention_events.length > 0 && (
        <div className="detail-block">
          <h3 className="detail-heading">需关注事件</h3>
          <ul className="event-list">
            {report.attention_events.map((ev, i) => (
              <li key={i} className="event-item">
                <span className={`badge ${ev.level === "red" ? "tone-danger" : "tone-warning"}`}>
                  {ev.level === "red" ? "红色" : "黄色"}
                </span>
                <span className="event-brief">{ev.brief}</span>
                <span className="event-time">{formatTime(ev.occurred_at)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="detail-block">
        <h3 className="detail-heading">用药情况（五类分列）</h3>
        <div className="med-grid">
          {MED_LABELS.map(({ key, label, tone }) => (
            <div key={key} className="med-cell">
              <span className={`badge tone-${tone}`}>{label}</span>
              <span className="med-count">{report.medication_stats[key]}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Day 9 F30（FR-RPT-04）：日报语音条——audio_url 非空才渲染，null 保持既有降级
          （不渲染空播放器）；直连静态文件（PRD §2.1 媒体分发：不经后端转发） */}
      {report.audio_url && (
        <div className="detail-block">
          <h3 className="detail-heading">语音播报</h3>
          {/* eslint-disable-next-line jsx-a11y/media-has-caption -- 语音条为完整日报朗读，无独立字幕轨 */}
          <audio controls preload="none" src={report.audio_url} className="audio-player" />
        </div>
      )}

      {report.health_summary && (
        <div className="detail-block">
          <h3 className="detail-heading">健康摘要</h3>
          <p className="detail-text">{report.health_summary}</p>
        </div>
      )}

      <div className="detail-block">
        <h3 className="detail-heading">今日摘要</h3>
        <p className="detail-text">{report.summary_text}</p>
      </div>

      <p className="detail-note">推送时间：{report.pushed_at ? formatTime(report.pushed_at) : "未推送"}</p>
    </article>
  );
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
