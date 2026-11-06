import { useEffect, useState } from "react";
import {
  ApiError,
  fetchBoundElders,
  logout,
  NETWORK_ERROR_TEXT,
  type BoundElder,
  type Principal,
} from "../api";
import { ReportsPage } from "./ReportsPage";
import { NotificationsPage } from "./NotificationsPage";

/**
 * 家属端应用壳（契约：docs/接口契约_家属端数据.md v1.0）。
 * 顶栏（标题 + 长者切换器 + 退出）+ 日报/通知两个 tab；375px 单列，无 router 库。
 * 长者切换器数据源 = F0 授权长者列表；切换后 ReportsPage 按 elderId 重载。
 */
interface HomePageProps {
  principal: Principal;
  onLogout: () => void;
}

type Tab = "reports" | "notifications";

export function HomePage({ principal, onLogout }: HomePageProps) {
  const [tab, setTab] = useState<Tab>("reports");
  const [elders, setElders] = useState<BoundElder[] | null>(null);
  const [elderId, setElderId] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // 通知详情跳转日报：跨 tab 打开指定日报（payload.report_id，契约 S-02）
  const [openReportId, setOpenReportId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchBoundElders()
      .then((items) => {
        if (cancelled) return;
        setElders(items);
        setElderId(items[0]?.elder_id ?? null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setLoadError(err instanceof ApiError ? err.message : NETWORK_ERROR_TEXT);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function handleLogout() {
    logout();
    onLogout();
  }

  function handleOpenReport(reportId: string) {
    setOpenReportId(reportId);
    setTab("reports");
  }

  const currentElder = elders?.find((e) => e.elder_id === elderId) ?? null;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-row">
          <span className="topbar-title">康伴 · 家属端</span>
          <button type="button" className="topbar-exit" onClick={handleLogout}>
            退出
          </button>
        </div>
        <div className="topbar-row">
          <label className="elder-switch">
            <span className="elder-switch-label">查看</span>
            {elders && elders.length > 0 ? (
              <select
                className="elder-select"
                value={elderId ?? ""}
                onChange={(e) => {
                  setElderId(e.target.value);
                  setOpenReportId(null);
                }}
              >
                {elders.map((e) => (
                  <option key={e.elder_id} value={e.elder_id}>
                    {e.name}（{e.relation}）
                  </option>
                ))}
              </select>
            ) : (
              <span className="elder-switch-empty">{loadError ?? "未绑定长者"}</span>
            )}
          </label>
        </div>
      </header>

      <nav className="tabs" aria-label="功能导航">
        <button
          type="button"
          className={`tab${tab === "reports" ? " tab-active" : ""}`}
          aria-current={tab === "reports" ? "page" : undefined}
          onClick={() => setTab("reports")}
        >
          健康日报
        </button>
        <button
          type="button"
          className={`tab${tab === "notifications" ? " tab-active" : ""}`}
          aria-current={tab === "notifications" ? "page" : undefined}
          onClick={() => setTab("notifications")}
        >
          通知中心
        </button>
      </nav>

      <main className="app-main">
        {tab === "reports" &&
          (elderId ? (
            <ReportsPage
              elderId={elderId}
              elderName={currentElder?.name ?? ""}
              openReportId={openReportId}
              onOpenReport={setOpenReportId}
              onCloseReport={() => setOpenReportId(null)}
            />
          ) : (
            <p className="empty-state">{loadError ?? "您还没有绑定长者，请联系机构工作人员"}</p>
          ))}
        {tab === "notifications" && <NotificationsPage onOpenReport={handleOpenReport} />}

        <p className="app-footer-note">
          {principal.name}，您好 · 数据为教学模拟信息，不构成医疗诊断
        </p>
      </main>
    </div>
  );
}
