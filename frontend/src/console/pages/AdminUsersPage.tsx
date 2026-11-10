/**
 * 管理员 · 用户与角色（Day 5 · D-1 / N2；Day 6 · N18 员工列表与改绑扩展）。
 *
 * 覆盖四流程中的三条：建员工（E12，含 elder_ids 分配 → 补齐 G2）、
 * 建家属账号（E15）、家属绑定长者（E13，≤2 位由服务端 409 兜底）。
 * Day 6 新增：员工列表（GET /api/staff 契约增补）+ 调整负责长者（E20 全量覆盖），
 * 改绑弹窗多选来自真实 E7，是否「最后一位责任护工」不做本地预判，以后端 409 为准。
 * Day 7（D2，P2-25/40）：长者候选改完整分页（page_size=100 循环取满 total，不再只取
 * 前 100 位）；保存只提交「勾选 ∪ 未进候选列表的已绑长者」，未列出的长者不被解除；
 * createStaff 成功后刷新员工列表；refreshStaffList 加请求序号防旧响应覆盖。
 * 设备建档见 AdminDevicesPage。
 *
 * 范围边界：不含 E12 PATCH 改角色/停用、教学数据重置、人设配置、审计日志（后续批次）。
 * 长者候选一律来自真实 E7（admin 全量），不臆造 elder_id（AGENTS §4 禁区 9）。
 * 表单校验前后端双层；错误文案用用户语言，不回显 trace_id 与内部细节。
 */

import { useEffect, useRef, useState } from "react";
import { ApiError, roleLabel } from "../api";
import {
  AdminApiError,
  bindGuardian,
  createGuardian,
  createStaff,
  listStaff,
  reassignElders,
  type GuardianCreated,
  type GuardianBinding,
  type StaffCreated,
  type StaffListItem,
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

function staffRoleLabel(role: StaffRole): string {
  return ROLE_OPTIONS.find((opt) => opt.value === role)?.label ?? role;
}

/** E7 完整分页拉取（P2-25）：page_size=100 循环取满 total，候选不再只取前 100 位。
 *  超过 100 位长者时第 101 位起必须可见可勾，否则全量覆盖改绑会误解除真实绑定。 */
const ELDERS_PAGE_SIZE = 100;
async function fetchAllElders(): Promise<ElderListItem[]> {
  const first = await listElders({ page: 1, pageSize: ELDERS_PAGE_SIZE });
  const items = [...first.items];
  const totalPages = Math.ceil(first.total / ELDERS_PAGE_SIZE);
  for (let page = 2; page <= totalPages; page++) {
    const res = await listElders({ page, pageSize: ELDERS_PAGE_SIZE });
    items.push(...res.items);
  }
  return items;
}

export function AdminUsersPage() {
  // 长者候选（真实 E7，admin 全量）
  const [elders, setElders] = useState<ElderListItem[] | null>(null);
  const [eldersError, setEldersError] = useState<string | null>(null);

  // 员工列表（GET /api/staff，Day 6 契约增补）
  const [staffList, setStaffList] = useState<StaffListItem[] | null>(null);
  const [staffListError, setStaffListError] = useState<string | null>(null);

  // 改绑弹窗（E20 全量覆盖）
  const [reassignTarget, setReassignTarget] = useState<StaffListItem | null>(null);
  const [reassignIds, setReassignIds] = useState<string[]>([]);
  const [reassignBusy, setReassignBusy] = useState(false);
  const [reassignError, setReassignError] = useState<string | null>(null);
  const [reassignResult, setReassignResult] = useState<{ name: string; added: number; removed: number } | null>(null);

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

  // 员工列表请求序号（P2-40）：并发刷新时旧响应不覆盖新状态
  const staffListSeqRef = useRef(0);

  // 员工列表本页检索与角色统计：纯前端过滤，不改 GET /api/staff 契约
  const [staffQuery, setStaffQuery] = useState("");
  const visibleStaff = (staffList ?? []).filter((s) => {
    const q = staffQuery.trim().toLowerCase();
    return !q || s.name.toLowerCase().includes(q) || s.phone_masked.toLowerCase().includes(q);
  });
  const countByRole = (role: StaffRole) =>
    (staffList ?? []).filter((s) => s.role === role).length;

  useEffect(() => {
    let cancelled = false;
    fetchAllElders()
      .then((items) => {
        if (cancelled) return;
        setElders(items);
        setEldersError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setEldersError(err instanceof ApiError ? err.message : "长者列表加载失败，请稍后重试");
      });
    listStaff()
      .then((items) => {
        if (cancelled) return;
        setStaffList(items);
        setStaffListError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setStaffListError(errorText(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /** 改绑成功后重拉员工列表（服务端为准，不本地推算差集）。
   *  请求序号防并发覆盖（P2-40）：连续快速刷新时只让最后一次请求生效。 */
  async function refreshStaffList() {
    const seq = ++staffListSeqRef.current;
    try {
      const items = await listStaff();
      if (seq !== staffListSeqRef.current) return; // 已有更新请求发出，丢弃旧响应
      setStaffList(items);
      setStaffListError(null);
    } catch (err) {
      if (seq !== staffListSeqRef.current) return;
      setStaffListError(errorText(err));
    }
  }

  function openReassign(s: StaffListItem) {
    setReassignResult(null);
    setReassignError(null);
    setReassignTarget(s);
    setReassignIds([...s.bound_elder_ids]);
  }

  function toggleReassignElder(elderId: string) {
    setReassignIds((prev) =>
      prev.includes(elderId) ? prev.filter((id) => id !== elderId) : [...prev, elderId],
    );
  }

  async function handleReassignSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!reassignTarget) return;
    setReassignError(null);
    setReassignBusy(true);
    try {
      // P2-25 差集口径：最终集合 = 用户勾选 ∪ 未进候选列表的已绑长者（如已归档，
      // 不参与勾选也不得被解除）。候选已完整分页，不存在「未加载的长者」；
      // 若 E7 过滤导致个别已绑长者不在候选中，此处原样保留，不会被误解除。
      const candidateIds = new Set((elders ?? []).map((e) => e.elder_id));
      const preservedBound = reassignTarget.bound_elder_ids.filter(
        (id) => !candidateIds.has(id),
      );
      const result = await reassignElders(reassignTarget.staff_id, [
        ...new Set([...reassignIds, ...preservedBound]),
      ]);
      setReassignResult({
        name: reassignTarget.name,
        added: result.added.length,
        removed: result.removed.length,
      });
      setReassignTarget(null);
      await refreshStaffList();
    } catch (err) {
      // 403/409 留在弹窗内提示，不关闭（用户可改选后重试）
      setReassignError(errorText(err));
    } finally {
      setReassignBusy(false);
    }
  }

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
      await refreshStaffList(); // P2-25：新员工立即出现在上方列表，不等下次进页
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
      <section className="admin-card" aria-labelledby="admin-staff-list-title">
        <header className="staff-head">
          <div>
            <h2 className="admin-card-title" id="admin-staff-list-title">
              员工列表
            </h2>
            <p className="staff-meta">
              共 {staffList?.length ?? 0} 位 · 护工 {countByRole("care_worker")} · 医生{" "}
              {countByRole("doctor")} · 管理员 {countByRole("admin")}
            </p>
          </div>
          {staffList && staffList.length > 0 && (
            <input
              className="input staff-search"
              type="search"
              value={staffQuery}
              placeholder="搜索姓名或手机号"
              aria-label="搜索员工姓名或手机号"
              onChange={(e) => setStaffQuery(e.target.value)}
            />
          )}
        </header>

        {/* 接口与全量覆盖口径留给开发/联调查阅，不占产品界面首屏 */}
        <details className="staff-devnote">
          <summary>接口与改绑口径</summary>
          <p>
            对应 GET /api/staff（Day 6 契约增补）；「调整负责长者」为全量覆盖（E20），保存后未勾选的长者将解除负责关系。
          </p>
        </details>
        {staffListError && (
          <p className="alert-danger" role="alert">
            {staffListError}
          </p>
        )}
        {!staffListError && !staffList && <p className="admin-empty">员工列表加载中…</p>}
        {staffList && staffList.length === 0 && <p className="admin-empty">暂无员工账号</p>}
        {staffList && staffList.length > 0 && visibleStaff.length > 0 && (
          <div className="staff-table-wrap">
            <table className="admin-table staff-table">
              <thead>
                <tr>
                  <th scope="col">姓名</th>
                  <th scope="col">角色</th>
                  <th scope="col">手机号</th>
                  <th scope="col" className="staff-num">负责长者数</th>
                  <th scope="col" className="staff-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                {visibleStaff.map((s) => (
                  <tr key={s.staff_id}>
                    <td className="staff-name">{s.name}</td>
                    <td>
                      <span className={`badge ${s.role === "admin" ? "badge-brand" : "badge-neutral"}`}>
                        {staffRoleLabel(s.role)}
                      </span>
                    </td>
                    <td className="admin-mono">{s.phone_masked}</td>
                    <td
                      className={`staff-num ${s.role !== "admin" && s.bound_elder_ids.length === 0 ? "is-zero" : ""}`}
                    >
                      {s.role === "admin"
                        ? "—"
                        : s.bound_elder_ids.length > 0
                          ? `${s.bound_elder_ids.length} 位`
                          : "未分配"}
                    </td>
                    <td className="staff-actions">
                      {s.role !== "admin" && (
                        <button
                          type="button"
                          className="btn-ghost btn-ghost-brand"
                          aria-label={`调整 ${s.name} 的负责长者`}
                          title={`调整 ${s.name} 的负责长者`}
                          onClick={() => openReassign(s)}
                        >
                          调整
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {staffList && staffList.length > 0 && visibleStaff.length === 0 && (
          <p className="staff-no-match">
            没有找到匹配的员工。
            <button type="button" className="btn-ghost btn-ghost-brand" onClick={() => setStaffQuery("")}>
              清空搜索
            </button>
          </p>
        )}
        {reassignResult && (
          <p className="admin-result" role="status">
            已更新 {reassignResult.name} 的负责长者：新增 {reassignResult.added} 位、移除{" "}
            {reassignResult.removed} 位。
          </p>
        )}
      </section>

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
              <span className="admin-field-label">
                已选择 {staffElderIds.length} 位 / 共 {elders?.length ?? 0} 位
              </span>
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

      {reassignTarget && (
        <div
          className="rv-mask"
          role="dialog"
          aria-modal="true"
          aria-label={`调整负责长者 · ${reassignTarget.name}`}
          onClick={() => !reassignBusy && setReassignTarget(null)}
        >
          <form className="rv-dialog" onClick={(e) => e.stopPropagation()} onSubmit={handleReassignSubmit}>
            <h3 className="wo-dialog-title">调整负责长者 · {reassignTarget.name}</h3>
            <p className="admin-card-sub">
              全量覆盖：保存后未勾选的长者将解除负责关系；若某长者因此失去最后一位责任护工，服务端会拒绝并提示。
              已选择 {reassignIds.length} 位 / 共 {elders?.length ?? 0} 位
              {reassignTarget.bound_elder_ids.some(
                (id) => !(elders ?? []).some((e) => e.elder_id === id),
              ) && "；未列出但已绑定的长者将保持不变"}
              。
            </p>
            {eldersError && <p className="field-error">{eldersError}</p>}
            {!eldersError && !elders && <p className="admin-empty">长者列表加载中…</p>}
            {elders && elders.length === 0 && <p className="admin-empty">暂无在住长者</p>}
            {elders && elders.length > 0 && (
              <div className="admin-check-list">
                {elders.map((e) => (
                  <label key={e.elder_id} className="admin-check">
                    <input
                      type="checkbox"
                      checked={reassignIds.includes(e.elder_id)}
                      onChange={() => toggleReassignElder(e.elder_id)}
                    />
                    {elderLabel(e)}
                  </label>
                ))}
              </div>
            )}
            {reassignError && (
              <p className="field-error" role="alert">
                {reassignError}
              </p>
            )}
            <div className="dlg-actions">
              <button
                type="button"
                className="btn-secondary"
                disabled={reassignBusy}
                onClick={() => setReassignTarget(null)}
              >
                取消
              </button>
              <button type="submit" className="btn-primary" disabled={reassignBusy || !elders}>
                {reassignBusy ? "正在保存…" : "保存"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
