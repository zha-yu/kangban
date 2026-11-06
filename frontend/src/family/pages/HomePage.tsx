import { logout, roleLabel, type Principal } from "../api";

/**
 * 家属端占位首页（task-05 F2：登录后占位，仅角色分流提示 + 退出按钮）。
 * 日报列表/详情、通知中心等业务页属前端页面方案 §3.4 第 4 批，不在本卡。
 * 家属端主体恒为 guardian（role 为 None），角色显示只依赖 kind/name。
 */
interface HomePageProps {
  principal: Principal;
  onLogout: () => void;
}

export function HomePage({ principal, onLogout }: HomePageProps) {
  function handleLogout() {
    logout();
    onLogout();
  }

  return (
    <div className="auth-page">
      <div className="auth-card home-card">
        <h1 className="auth-title">
          登录成功 · {roleLabel(principal)}：{principal.name}
        </h1>
        <p className="auth-subtitle">
          您绑定的长者信息与每日日报属后续批次页面，当前为登录链路占位页。
        </p>
        <div className="auth-actions">
          {/* 次按钮形态：surface 底 + 1px line 描边 + ink 字（前端页面方案 §4.2） */}
          <button type="button" className="btn-secondary" onClick={handleLogout}>
            退出登录
          </button>
        </div>
      </div>
    </div>
  );
}
