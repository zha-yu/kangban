/**
 * 管理员 · 用户与角色（Day 5 · D-1 / N2）。
 *
 * 覆盖四流程中的三条：建员工（E12，含 elder_ids 分配 → 补齐 G2）、
 * 建家属账号（E15）、家属绑定长者（E13，≤2 位由服务端 409 兜底）。
 * 设备建档见 AdminDevicesPage。
 *
 * 范围边界：不含 E12 PATCH 改角色/停用、教学数据重置、人设配置、审计日志（后续批次）。
 * 长者候选一律来自真实 E7（admin 全量），不臆造 elder_id（AGENTS §4 禁区 9）。
 * 表单校验前后端双层；错误文案用用户语言，不回显 trace_id 与内部细节。
 */

import { useEffect, useState } from "react";
import { ApiError, roleLabel } from "../api";
import {
  AdminApiError,
  bindGuardian,
  createGuardian,
  createStaff,
  type GuardianCreated,
  type GuardianBinding,
  type StaffCreated,
  type StaffRole,
} from "../admin-api";
import { listElders, type ElderListItem } from "../elders-api";

const PHONE_RE = /^1\d{10}$/;
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

const ROLE_OPTIONS: Array<{ value: StaffRole; label: string }> = [
  { value: "care_worker", label: "护工" },
  { value: "doctor", label: "医生" },
  { value: "admin", label: "管理员" },
];

function errorText(err: unknown): string {
  if (err instanceof AdminApiError || err instanceof ApiError) return err.message;
  return "操作失败，请稍后重试";
}

function accountError(name: string, phone: string, password: string): string | null {
  if (!name.trim()) return "请填写姓名";
  if (!PHONE_RE.test(phone.trim())) return "请输入 11 位手机号";
  if (password.length < 8 || password.length > 128) return "口令长度需为 8–128 位";
  return null;
}

function elderLabel(e: ElderListItem): string {
  if (e.room_no) return `${e.name}（${e.room_no} ${e.bed_no ?? ""}）`;
  return `${e.name}（未入床）`;
}

export function AdminUsersPage() {
  // 长者候选（真实 E7，admin 全量）
  const [elders, setElders] = useState<ElderListItem[] | null>(null);
  const [eldersError, setEldersError] = useState<string | null>(null);

  // 建员工
  const [staffName, setStaffName] = useState("");
  const [staffRole, setStaffRole] = useState<StaffRole>("care_worker");
  const [staffPhone, setStaffPhone] = useState("");
  const [staffPassword, setStaffPassword] = useState("");
  const [staffElderIds, setStaffElderIds] = useState<string[]>([]);
  const [staffBusy, setStaffBusy] = useState(false);
  const [staffError, setStaffError] = useState<string | null>(null);
  const [staffResult, setStaffResult] = useState<StaffCreated | null>(null);
  const [staffAssignedCount, setStaffAssignedCount] = useState(0);

  // 建家属
  const [guardianName, setGuardianName] = useState("");
  const [guardianPhone, setGuardianPhone] = useState("");
  const [guardianPassword, setGuardianPassword] = useState("");
  const [guardianBusy, setGuardianBusy] = useState(false);
  const [guardianError, setGuardianError] = useState<string | null>(null);
  const [guardianResult, setGuardianResult] = useState<GuardianCreated | null>(null);

  // 绑定长者
  const [bindElderId, setBindElderId] = useState("");
  const [bindGuardianId, setBindGuardianId] = useState("");
  const [bindRelation, setBindRelation] = useState("");
  const [bindBusy, setBindBusy] = useState(false);
  const [bindError, setBindError] = useState<string | null>(null);
  const [bindResult, setBindResult] = useState<GuardianBinding | null>(null);

  useEffect(() => {
    let cancelled = false;
    listElders({ page: 1, pageSize: 100 })
      .then((res) => {
        if (cancelled) return;
        setElders(res.items);
        setEldersError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setEldersError(err instanceof ApiError ? err.message : "长者列表加载失败，请稍后重试");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function toggleStaffElder(elderId: string) {
    setStaffElderIds((prev) =>
      prev.includes(elderId) ? prev.filter((id) => id !== elderId) : [...prev, elderId],
    );
  }

  async function handleStaffSubmit(e: React.FormEvent) {
    e.preventDefault();
    setStaffError(null);
    setStaffResult(null);
    const invalid = accountError(staffName, staffPhone, staffPassword);
    if (invalid) {
      setStaffError(invalid);
      return;
    }
    setStaffBusy(true);
    try {
      const created = await createStaff({
        name: staffName.trim(),
        role: staffRole,
        phone: staffPhone.trim(),
        password: staffPassword,
        // 管理员不绑长者（身份契约 E12：给 admin 分配长者一律 403）
        ...(staffRole !== "admin" && staffElderIds.length > 0 ? { elder_ids: staffElderIds } : {}),
      });
      setStaffResult(created);
      setStaffAssignedCount(staffElderIds.length);
      setStaffName("");
      setStaffPhone("");
      setStaffPassword("");
      setStaffElderIds([]);
    } catch (err) {
      setStaffError(errorText(err));
    } finally {
      setStaffBusy(false);
    }
  }

  async function handleGuardianSubmit(e: React.FormEvent) {
    e.preventDefault();
    setGuardianError(null);
    setGuardianResult(null);
    const invalid = accountError(guardianName, guardianPhone, guardianPassword);
    if (invalid) {
      setGuardianError(invalid);
      return;
    }
    setGuardianBusy(true);
    try {
      const created = await createGuardian({
        name: guardianName.trim(),
        phone: guardianPhone.trim(),
        password: guardianPassword,
      });
      setGuardianResult(created);
      setBindGuardianId(created.guardian_id);
      setGuardianName("");
      setGuardianPhone("");
      setGuardianPassword("");
    } catch (err) {
      setGuardianError(errorText(err));
    } finally {
      setGuardianBusy(false);
    }
  }

  async function handleBindSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBindError(null);
    setBindResult(null);
    if (!bindElderId) {
      setBindError("请选择长者");
      return;
    }
    if (!UUID_RE.test(bindGuardianId.trim())) {
      setBindError("请填写有效的家属 ID（guardian_id）");
      return;
    }
    if (!bindRelation.trim()) {
      setBindError("请填写与长者的关系");
      return;
    }
    if (bindRelation.trim().length > 32) {
      setBindError("关系描述最多 32 个字");
      return;
    }
    setBindBusy(true);
    try {
      const binding = await bindGuardian(bindElderId, bindGuardianId.trim(), bindRelation.trim());
      setBindResult(binding);
      setBindRelation("");
    } catch (err) {
      setBindError(errorText(err));
    } finally {
      setBindBusy(false);
    }
  }

  return (
    <div className="admin-page">
      <section className="admin-card" aria-labelledby="admin-staff-title">
        <h2 className="admin-card-title" id="admin-staff-title">
          员工建档
        </h2>
        <p className="admin-card-sub">对应 E12；仅管理员可操作。口令只提交给服务端，不在页面回显。</p>
        <form className="admin-form" onSubmit={handleStaffSubmit}>
          <div className="admin-field">
            <label htmlFor="staff-name">姓名</label>
            <input
              id="staff-name"
              className="input"
              value={staffName}
              maxLength={64}
              onChange={(e) => setStaffName(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div className="admin-field">
            <label htmlFor="staff-role">角色</label>
            <select
              id="staff-role"
              className="input"
              value={staffRole}
              onChange={(e) => {
                const next = e.target.value as StaffRole;
                setStaffRole(next);
                if (next === "admin") setStaffElderIds([]);
              }}
            >
              {ROLE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
          <div className="admin-field">
            <label htmlFor="staff-phone">手机号</label>
            <input
              id="staff-phone"
              className="input"
              inputMode="numeric"
              value={staffPhone}
              maxLength={11}
              placeholder="11 位手机号"
              onChange={(e) => setStaffPhone(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div className="admin-field">
            <label htmlFor="staff-password">登录口令</label>
            <input
              id="staff-password"
              className="input"
              type="password"
              value={staffPassword}
              maxLength={128}
              placeholder="8–128 位"
              onChange={(e) => setStaffPassword(e.target.value)}
              autoComplete="new-password"
            />
          </div>

          {staffRole !== "admin" && (
            <div className="admin-checks">
              <span className="admin-field-label">分配长者（可留空，留空表示暂不分配）</span>
              {eldersError && <p className="helper-error">{eldersError}</p>}
              {!eldersError && !elders && <p className="admin-empty">长者列表加载中…</p>}
              {elders && elders.length === 0 && <p className="admin-empty">暂无在住长者</p>}
              {elders && elders.length > 0 && (
                <div className="admin-check-list">
                  {elders.map((e) => (
                    <label key={e.elder_id} className="admin-check">
                      <input
                        type="checkbox"
                        checked={staffElderIds.includes(e.elder_id)}
                        onChange={() => toggleStaffElder(e.elder_id)}
                      />
                      {elderLabel(e)}
                    </label>
                  ))}
                </div>
              )}
            </div>
          )}

          {staffError && (
            <p className="alert-danger" role="alert">
              {staffError}
            </p>
          )}
          {staffResult && (
            <p className="admin-result" role="status">
              已创建{roleLabel({ kind: "staff", id: staffResult.staff_id, role: staffResult.role, name: staffResult.name })}
              ：{staffResult.name}（{staffResult.phone_masked}）。本次分配长者：{staffAssignedCount} 位。
            </p>
          )}

          <div className="admin-actions">
            <button type="button" className="btn-secondary" onClick={() => setStaffElderIds([])}>
              清空已选长者
            </button>
            <button type="submit" className="btn-primary" disabled={staffBusy}>
              {staffBusy ? "正在创建…" : "创建员工"}
            </button>
          </div>
        </form>
      </section>

      <section className="admin-card" aria-labelledby="admin-guardian-title">
        <h2 className="admin-card-title" id="admin-guardian-title">
          家属建号
        </h2>
        <p className="admin-card-sub">
          对应 E15；家属账号只能由管理员创建。建号成功后下方「家属绑定长者」会自动填入家属 ID。
        </p>
        <form className="admin-form" onSubmit={handleGuardianSubmit}>
          <div className="admin-field">
            <label htmlFor="guardian-name">姓名</label>
            <input
              id="guardian-name"
              className="input"
              value={guardianName}
              maxLength={64}
              onChange={(e) => setGuardianName(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div className="admin-field">
            <label htmlFor="guardian-phone">手机号</label>
            <input
              id="guardian-phone"
              className="input"
              inputMode="numeric"
              value={guardianPhone}
              maxLength={11}
              placeholder="11 位手机号"
              onChange={(e) => setGuardianPhone(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div className="admin-field">
            <label htmlFor="guardian-password">登录口令</label>
            <input
              id="guardian-password"
              className="input"
              type="password"
              value={guardianPassword}
              maxLength={128}
              placeholder="8–128 位"
              onChange={(e) => setGuardianPassword(e.target.value)}
              autoComplete="new-password"
            />
          </div>

          {guardianError && (
            <p className="alert-danger" role="alert">
              {guardianError}
            </p>
          )}
          {guardianResult && (
            <p className="admin-result" role="status">
              已创建家属账号：{guardianResult.name}（{guardianResult.phone_masked}），家属 ID：
              <span className="admin-mono">{guardianResult.guardian_id}</span>
            </p>
          )}

          <div className="admin-actions">
            <button type="submit" className="btn-primary" disabled={guardianBusy}>
              {guardianBusy ? "正在创建…" : "创建家属账号"}
            </button>
          </div>
        </form>
      </section>

      <section className="admin-card" aria-labelledby="admin-bind-title">
        <h2 className="admin-card-title" id="admin-bind-title">
          家属绑定长者
        </h2>
        <p className="admin-card-sub">
          对应 E13；每位长者最多 2 位在效监护人，超限或重复绑定由服务端返回 409。
        </p>
        <form className="admin-form" onSubmit={handleBindSubmit}>
          <div className="admin-field">
            <label htmlFor="bind-elder">长者</label>
            <select
              id="bind-elder"
              className="input"
              value={bindElderId}
              onChange={(e) => setBindElderId(e.target.value)}
            >
              <option value="">请选择长者</option>
              {(elders ?? []).map((e) => (
                <option key={e.elder_id} value={e.elder_id}>
                  {elderLabel(e)}
                </option>
              ))}
            </select>
          </div>
          <div className="admin-field">
            <label htmlFor="bind-guardian">家属 ID（guardian_id）</label>
            <input
              id="bind-guardian"
              className="input"
              value={bindGuardianId}
              placeholder="E15 创建后返回的 UUID"
              onChange={(e) => setBindGuardianId(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div className="admin-field">
            <label htmlFor="bind-relation">与长者关系</label>
            <input
              id="bind-relation"
              className="input"
              value={bindRelation}
              maxLength={32}
              placeholder="如：女儿"
              onChange={(e) => setBindRelation(e.target.value)}
              autoComplete="off"
            />
          </div>

          {bindError && (
            <p className="alert-danger" role="alert">
              {bindError}
            </p>
          )}
          {bindResult && (
            <p className="admin-result" role="status">
              已绑定：家属 {bindResult.guardian_id} → 长者 {bindResult.elder_id}（关系：{bindResult.relation}，状态：
              {bindResult.status === "active" ? "生效中" : "已解绑"}）
            </p>
          )}

          <div className="admin-actions">
            <button type="submit" className="btn-primary" disabled={bindBusy}>
              {bindBusy ? "正在绑定…" : "绑定长者"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
