import { logout, roleLabel, type Principal } from "../api";

/**
 * 照护台应用骨架（task-06 F2）：浅色侧栏 + 顶栏 + 主区。
 *
 * 口径（前端页面方案 §3.2 通用）：侧栏浅色同族 + 主色 8% 底选中态，不用深侧栏；
 * 本卡只做骨架——护工「工单看板」一项可用，其余业务项与医生/管理员项置灰占位，
 * 后续批次按角色逐项挂页（f2 计划 D2）。
 */

/** 侧栏菜单项：label + 可用性；enabled 仅当前批次已交付项 */
interface NavItem {
  key: string;
  label: string;
  enabled: boolean;
}

/** 按前端页面方案 §3.2 三角色职责拆分；顺序即页面方案行文顺序。
    enabled 跟随批次交付进度：task-06 工单看板、task-07 负责长者已交付。 */
const NAV_BY_ROLE: Record<string, NavItem[]> = {
  care_worker: [
    { key: "workorders", label: "工单看板", enabled: true },
    { key: "elders", label: "负责长者", enabled: true }, // task-07 交付
    { key: "health-form", label: "健康代填", enabled: false },
    { key: "medication", label: "药单只读", enabled: false },
    { key: "alerts", label: "告警通知中心", enabled: false },
  ],
  doctor: [
    { key: "review-queue", label: "复核队列", enabled: false },
    { key: "trend", label: "健康趋势", enabled: false },
    { key: "medication-edit", label: "药单维护", enabled: false },
    { key: "drug-questions", label: "药品疑问", enabled: false },
  ],
  admin: [
    { key: "users", label: "用户与角色", enabled: false },
    { key: "reset", label: "教学数据重置", enabled: false },
    { key: "persona", label: "人设配置", enabled: false },
    { key: "audit", label: "审计日志", enabled: false },
  ],
};

interface AppLayoutProps {
  principal: Principal;
  activeKey: string;
  /** 主区内容；医生/管理员批次未到时由 App 传占位内容 */
  children: React.ReactNode;
  onLogout: () => void;
  /** 已交付菜单项点击回调（task-07：侧栏页面间切换） */
  onNavigate?: (key: string) => void;
}

export function AppLayout({ principal, activeKey, children, onLogout, onNavigate }: AppLayoutProps) {
  const items = NAV_BY_ROLE[principal.role ?? ""] ?? [];
  const enabledItems = items.filter((it) => it.enabled);

  function handleLogout() {
    logout();
    onLogout();
  }

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <div className="app-sidebar-brand">康伴 · 照护台</div>
        <nav className="app-sidebar-nav" aria-label="照护台导航">
          {items.map((it) =>
            it.enabled ? (
              <a
                key={it.key}
                href="#"
                className={`app-nav-item${it.key === activeKey ? " is-active" : ""}`}
                aria-current={it.key === activeKey ? "page" : undefined}
                onClick={(e) => {
                  e.preventDefault();
                  onNavigate?.(it.key);
                }}
              >
                {it.label}
              </a>
            ) : (
              <span key={it.key} className="app-nav-item is-disabled" aria-disabled="true" title="后续批次交付">
                {it.label}
              </span>
            ),
          )}
        </nav>
        {/* 侧栏底部：当前登录身份 */}
        <div className="app-sidebar-foot">
          <div className="app-sidebar-user">{principal.name}</div>
          <div className="app-sidebar-role">{roleLabel(principal)}</div>
        </div>
      </aside>

      <div className="app-main">
        <header className="app-topbar">
          <div className="app-topbar-title">{enabledItems.find((it) => it.key === activeKey)?.label ?? "照护台"}</div>
          <div className="app-topbar-actions">
            <span className="app-topbar-user">
              {principal.name} · {roleLabel(principal)}
            </span>
            {/* 次按钮形态：surface 底 + 1px line 描边 + ink 字（前端页面方案 §4.2） */}
            <button type="button" className="btn-secondary" onClick={handleLogout}>
              退出登录
            </button>
          </div>
        </header>
        <main className="app-content">{children}</main>
      </div>
    </div>
  );
}
