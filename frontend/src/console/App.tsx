import { useEffect, useState } from "react";
import {
  ApiError,
  cachedProfile,
  fetchCurrent,
  hasSession,
  NETWORK_ERROR_TEXT,
  type Principal,
} from "./api";
import { LoginPage } from "./pages/LoginPage";
import { HomePage } from "./pages/HomePage";

/**
 * 照护台入口（task-05 F1）：登录态路由。
 * 无本地登录态 → 登录页；有 → fetchCurrent 恢复身份（过期自动 refresh），
 * 恢复失败（refresh 也失效 / 网络错）→ 回登录页。
 */
type View = "loading" | "login" | "home";

export default function App() {
  const [view, setView] = useState<View>("loading");
  const [principal, setPrincipal] = useState<Principal | null>(null);

  useEffect(() => {
    if (!hasSession()) {
      setView("login");
      return;
    }
    // 先用本地缓存的角色渲染，再向服务端核实（fetchCurrent，E3）
    setPrincipal(cachedProfile());
    fetchCurrent()
      .then((identity) => {
        setPrincipal(identity);
        setView("home");
      })
      .catch((err: unknown) => {
        // ApiError invalid：refresh 已失效且登录态已清；network：后端不可用时也应能重登
        void (err instanceof ApiError ? err.message : NETWORK_ERROR_TEXT);
        setPrincipal(null);
        setView("login");
      });
  }, []);

  if (view === "loading") {
    return (
      <div className="auth-page">
        <div className="spinner" role="status" aria-label="正在恢复登录态" />
      </div>
    );
  }

  if (view === "home" && principal) {
    return <HomePage principal={principal} onLogout={() => setView("login")} />;
  }

  return (
    <LoginPage
      onSuccess={(p) => {
        setPrincipal(p);
        setView("home");
      }}
    />
  );
}
