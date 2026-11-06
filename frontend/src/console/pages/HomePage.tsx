import { logout, roleLabel, type Principal } from "../api";

/**
 * 员工端占位首页（task-05 F1：登录后占位，仅角色分流提示 + 退出按钮）。
 * 登录后的业务页（工单看板、复核队列、药单维护等）属前端页面方案 §3.4 第 3 批，不在本卡。
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
        <h1 className="auth-title">登录成功 · 角色：{roleLabel(principal)}</h1>
        <p className="auth-subtitle">欢迎您，{principal.name}</p>
        <p className="auth-note">
          工单看板、健康复核等业务页面属后续批次，当前为登录链路占位页。
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
