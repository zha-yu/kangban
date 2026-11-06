import { useEffect, useState } from "react";
import { ApiError } from "../api";
import {
  ELDER_STATUS_LABELS,
  getElder,
  listElders,
  type ElderDetail,
  type ElderListItem,
  type ElderStatus,
} from "../elders-api";

/**
 * 负责长者列表与详情（task-07 F3，console 首个真实对接业务页）。
 *
 * 口径：E7 分页列表（服务端按角色范围过滤，前端不做二次过滤）+ 状态筛选；
 * E6 详情 R4 全字段 + 房间床号（S-05），行点击在下方展开详情卡（f3 D3）；
 * 错误统一文案不回显 trace_id（task-05 既有约定）。
 */

const PAGE_SIZE = 20;

const GENDER_LABELS: Record<string, string> = {
  female: "女",
  male: "男",
};

const statusClass: Record<ElderStatus, string> = {
  active: "badge badge-success",
  away: "badge badge-warning",
  transferred: "badge badge-neutral",
  archived: "badge badge-neutral",
};

const TIME_FMT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

function fmtDate(iso: string): string {
  return TIME_FMT.format(new Date(iso));
}

/** birth_date（date 字符串）→ 年龄，空值返回 — */
function ageFrom(birthDate: string | null): string {
  if (!birthDate) return "—";
  const birth = new Date(birthDate);
  if (Number.isNaN(birth.getTime())) return "—";
  const now = new Date();
  let age = now.getFullYear() - birth.getFullYear();
  const beforeBirthday =
    now.getMonth() < birth.getMonth() ||
    (now.getMonth() === birth.getMonth() && now.getDate() < birth.getDate());
  if (beforeBirthday) age -= 1;
  return `${age} 岁`;
}

const STATUS_OPTIONS = Object.entries(ELDER_STATUS_LABELS) as [ElderStatus, string][];

function DetailCard({ detail }: { detail: ElderDetail }) {
  const rows: [string, string][] = [
    ["姓名", detail.name],
    ["性别", detail.gender ? (GENDER_LABELS[detail.gender] ?? detail.gender) : "—"],
    ["出生日期", detail.birth_date ?? "—"],
    ["年龄", ageFrom(detail.birth_date)],
    ["房间", detail.room_no ?? "—"],
    ["床位", detail.bed_no ?? "—"],
    ["状态", ELDER_STATUS_LABELS[detail.status]],
    ["人设 ID", detail.persona_id ?? "—"],
    ["会话序号", String(detail.session_seq)],
    ["建档时间", fmtDate(detail.created_at)],
  ];
  return (
    <dl className="elder-detail">
      {rows.map(([k, v]) => (
        <div key={k} className="elder-detail-row">
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function EldersPage() {
  const [items, setItems] = useState<ElderListItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<ElderStatus | "all">("all");
  const [error, setError] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<ReadonlySet<string>>(new Set());
  const [details, setDetails] = useState<Record<string, ElderDetail | undefined>>({});
  const [loadingIds, setLoadingIds] = useState<ReadonlySet<string>>(new Set());
  const [detailErrors, setDetailErrors] = useState<Record<string, string | undefined>>({});

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setError(null);
    const query = {
      page,
      pageSize: PAGE_SIZE,
      status: statusFilter === "all" ? undefined : statusFilter,
    };
    listElders(query)
      .then((r) => {
        if (cancelled) return;
        setItems(r.items);
        setTotal(r.total);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "长者列表加载失败，请稍后重试");
      });
    return () => {
      cancelled = true;
    };
  }, [page, statusFilter]);

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  async function toggleDetail(elderId: string) {
    // 已展开 → 收起（其他详情保持不动，支持多条同时展开）
    if (expandedIds.has(elderId)) {
      setExpandedIds((prev) => {
        const next = new Set(prev);
        next.delete(elderId);
        return next;
      });
      return;
    }
    setExpandedIds((prev) => new Set(prev).add(elderId));
    setDetailErrors((prev) => ({ ...prev, [elderId]: undefined }));
    if (details[elderId]) return; // 已缓存，不重复请求
    setLoadingIds((prev) => new Set(prev).add(elderId));
    try {
      const detail = await getElder(elderId);
      setDetails((prev) => ({ ...prev, [elderId]: detail }));
    } catch (err) {
      setDetailErrors((prev) => ({
        ...prev,
        [elderId]: err instanceof ApiError ? err.message : "详情加载失败，请稍后重试",
      }));
    } finally {
      setLoadingIds((prev) => {
        const next = new Set(prev);
        next.delete(elderId);
        return next;
      });
    }
  }

  return (
    <div className="elder-page">
      <div className="wo-filters" role="group" aria-label="长者筛选">
        <label className="wo-filter">
          <span>状态</span>
          <select
            className="field-input"
            value={statusFilter}
            onChange={(e) => {
              setPage(1);
              setStatusFilter(e.target.value as ElderStatus | "all");
            }}
          >
            <option value="all">全部状态</option>
            {STATUS_OPTIONS.map(([v, label]) => (
              <option key={v} value={v}>{label}</option>
            ))}
          </select>
        </label>
        <span className="wo-count">共 {total} 位长者</span>
      </div>

      {error && <p className="wo-load-error">{error}</p>}
      {!items && !error && <p className="wo-load-hint">正在加载长者列表…</p>}

      {items && (
        <ul className="wo-list">
          {items.map((e) => {
            const expanded = expandedIds.has(e.elder_id);
            return (
              <li key={e.elder_id} className="wo-item">
                <div className="wo-row">
                  <div className="wo-title">
                    <span className="wo-type">{e.name}</span>
                    <span className="wo-elder">
                      {e.room_no ?? "—"} · {e.bed_no ?? "—"}
                    </span>
                  </div>
                  <div className="wo-badges">
                    <span className={statusClass[e.status]}>{ELDER_STATUS_LABELS[e.status]}</span>
                  </div>
                  <div className="wo-actions">
                    <button
                      type="button"
                      className="btn-secondary btn-sm"
                      aria-expanded={expanded}
                      onClick={() => toggleDetail(e.elder_id)}
                    >
                      {expanded ? "收起详情" : "详情"}
                    </button>
                  </div>
                </div>
                {expanded && (
                  <div className="wo-detail">
                    {loadingIds.has(e.elder_id) && <p className="wo-load-hint">正在加载详情…</p>}
                    {detailErrors[e.elder_id] && <p className="wo-load-error">{detailErrors[e.elder_id]}</p>}
                    {details[e.elder_id] && <DetailCard detail={details[e.elder_id]!} />}
                  </div>
                )}
              </li>
            );
          })}
          {items.length === 0 && <li className="wo-empty">当前条件下没有负责的长者</li>}
        </ul>
      )}

      {pageCount > 1 && (
        <nav className="elder-pager" aria-label="分页">
          <button
            type="button"
            className="btn-secondary btn-sm"
            disabled={page <= 1}
            onClick={() => setPage((p) => p - 1)}
          >
            上一页
          </button>
          <span className="elder-pager-info">
            第 {page} / {pageCount} 页
          </span>
          <button
            type="button"
            className="btn-secondary btn-sm"
            disabled={page >= pageCount}
            onClick={() => setPage((p) => p + 1)}
          >
            下一页
          </button>
        </nav>
      )}
    </div>
  );
}
