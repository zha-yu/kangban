import { useEffect, useState, type ComponentType } from "react";
import {
  ApiError,
  cachedProfile,
  fetchCurrent,
  hasSession,
  NETWORK_ERROR_TEXT,
  roleLabel,
  type Principal,
} from "./api";
import { LoginPage } from "./pages/LoginPage";
import { AppLayout } from "./layout/AppLayout";
import { WorkOrdersPage } from "./pages/WorkOrdersPage";
import { EldersPage } from "./pages/EldersPage";
import { AlertsPage } from "./pages/AlertsPage";
import { HealthFormPage } from "./pages/HealthFormPage";
import { MedicationPage } from "./pages/MedicationPage";
import { MedicationEditPage } from "./pages/MedicationEditPage";
import { ReviewQueuePage } from "./pages/ReviewQueuePage";
import { AdminUsersPage } from "./pages/AdminUsersPage";
import { AdminRoomsPage } from "./pages/AdminRoomsPage";
import { AdminDevicesPage } from "./pages/AdminDevicesPage";
import { AdminLifecyclePage } from "./pages/AdminLifecyclePage";
import { AdminMemoryPage } from "./pages/AdminMemoryPage";
import { AdminStatsPage } from "./pages/AdminStatsPage";

/**
 * 照护台入口：登录态路由 + 角色分流（task-06～11 F2–F7）。
 * 无本地登录态 → 登录页；有 → fetchCurrent 恢复身份（过期自动 refresh），
 * 恢复失败（refresh 也失效 / 网络错）→ 回登录页。
 *
 * 分流口径（前端页面方案 §3.2，业务页按批次交付）：
 * - care_worker → 侧栏骨架 + 工单看板（Mock）/ 负责长者（真实对接 E7/E6）
 *   / 告警通知中心（Mock）/ 健康代填（提交 Mock + 长者下拉真实 E7）/ 药单只读（Mock）；
 * - doctor → 复核队列（Mock，task-11）/ 药单维护（Mock，task-12），其余医生项仍置灰；
 * - admin → 用户与角色 / 模拟设备（Day 5 D-1，真实端点 E12/E15/E13/E18/E16/E17），
 *   教学重置与退房 / 记忆管理 / 统计与审计（Day 9 C6；A4/X5 未交付时页面内提示），
 *   人设配置仍置灰（X6 作废）；
 * - guardian → 家属应走 family 入口，此处仅占位提示。
 */
type View = "loading" | "login" | "home";
type CareWorkerView = "workorders" | "elders" | "alerts" | "health-form" | "medication";

/** 护工侧栏视图 → 页面组件（映射表查表，不用三元链；键与 AppLayout NAV_BY_ROLE 的 key 对齐） */
const CARE_PAGE_BY_VIEW: Record<CareWorkerView, ComponentType<{ principal: Principal }>> = {
  workorders: WorkOrdersPage,
  elders: EldersPage,
  alerts: AlertsPage,
  "health-form": HealthFormPage,
  medication: MedicationPage,
};

type DoctorView = "review-queue" | "medication-edit";
type AdminView = "users" | "rooms" | "devices" | "reset" | "memory" | "stats";

/** admin 侧栏视图 → 页面组件（映射表查表，不用三元链；键与 AppLayout NAV_BY_ROLE 的 key 对齐） */
const ADMIN_PAGE_BY_VIEW: Record<AdminView, ComponentType> = {
  users: AdminUsersPage,
  rooms: AdminRoomsPage,
  devices: AdminDevicesPage,
  reset: AdminLifecyclePage,
  memory: AdminMemoryPage,
  stats: AdminStatsPage,
};

function PlaceholderHome({ principal }: { principal: Principal }) {
  return (
    <div className="app-placeholder">
      <h1>欢迎您，{principal.name}（{roleLabel(principal)}）</h1>
      <p>该角色的业务页面属前端页面方案第 3/4 批，尚未开工；当前为登录链路占位提示。</p>
    </div>
  );
}

export default function App() {
  const [view, setView] = useState<View>("loading");
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [careView, setCareView] = useState<CareWorkerView>("workorders");
  const [doctorView, setDoctorView] = useState<DoctorView>("review-queue");
  const [adminView, setAdminView] = useState<AdminView>("users");

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
    const handleLogout = () => setView("login");
    if (principal.kind === "staff" && principal.role === "care_worker") {
      const CarePage = CARE_PAGE_BY_VIEW[careView];
      return (
        <AppLayout
          principal={principal}
          activeKey={careView}
          onLogout={handleLogout}
          onNavigate={(key) => setCareView(key as CareWorkerView)}
        >
          <CarePage principal={principal} />
        </AppLayout>
      );
    }
    if (principal.kind === "staff" && principal.role === "doctor") {
      return (
        <AppLayout
          principal={principal}
          activeKey={doctorView}
          onLogout={handleLogout}
          onNavigate={(key) => setDoctorView(key as DoctorView)}
        >
          {doctorView === "medication-edit" ? <MedicationEditPage /> : <ReviewQueuePage principal={principal} />}
        </AppLayout>
      );
    }
    if (principal.kind === "staff" && principal.role === "admin") {
      const AdminPage = ADMIN_PAGE_BY_VIEW[adminView];
      return (
        <AppLayout
          principal={principal}
          activeKey={adminView}
          onLogout={handleLogout}
          onNavigate={(key) => setAdminView(key as AdminView)}
        >
          <AdminPage />
        </AppLayout>
      );
    }
    return (
      <AppLayout principal={principal} activeKey="" onLogout={handleLogout}>
        <PlaceholderHome principal={principal} />
      </AppLayout>
    );
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
