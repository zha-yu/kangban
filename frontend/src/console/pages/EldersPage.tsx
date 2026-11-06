import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api";
import {
  ELDER_STATUS_LABELS,
  assignBed,
  bindDevice,
  createDevice,
  createElder,
  fetchDevices,
  fetchRooms,
  getElder,
  listElders,
  type DeviceCreated,
  type DeviceItem,
  type ElderDetail,
  type ElderListItem,
  type ElderStatus,
  type RoomWithBeds,
} from "../elders-api";

/**
 * 负责长者列表与详情（task-07 F3，console 首个真实对接业务页）。
 *
 * 口径：E7 分页列表（服务端按角色范围过滤，前端不做二次过滤）+ 状态筛选；
 * E6 详情 R4 全字段 + 房间床号（S-05），行点击在下方展开详情卡（f3 D3）；
 * task-08 F2：页头「注册长者」按钮 → 四步向导（E5 建档 → E19 入床 →
 * E16 建设备 / E9 绑定 → 完成后引导去 pad 录声纹，契约 v1.8）；
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

function DetailCard({
  detail,
  onAssign,
}: {
  detail: ElderDetail;
  onAssign?: () => void;
}) {
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
    <div>
      {onAssign && !detail.room_no && (
        <div className="wiz-assign-hint">
          <span>该长者还没有床位与设备。</span>
          <button type="button" className="btn-primary btn-sm" onClick={onAssign}>
            分配床位/设备
          </button>
        </div>
      )}
      <dl className="elder-detail">
        {rows.map(([k, v]) => (
          <div key={k} className="elder-detail-row">
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 注册向导（task-08 F2 · E5 → E19 → E16/E9 → 完成引导，契约 v1.8）
// ---------------------------------------------------------------------------

const WIZ_STEP_LABELS = ["建档", "分配床位", "分配设备", "完成"] as const;

interface WizardState {
  step: number; // 1 建档 · 2 床位 · 3 设备 · 4 完成
  elderId: string | null;
  elderName: string;
  roomId: string | null;
  bedId: string | null;
  deviceId: string | null;
  secret: string | null; // 仅新建设备时的一次性明文
}

function stepsBar(step: number) {
  return (
    <ol className="wiz-steps" aria-label="注册步骤">
      {WIZ_STEP_LABELS.map((label, i) => (
        <li
          key={label}
          className={i + 1 === step ? "wiz-step wiz-step-active" : "wiz-step"}
          aria-current={i + 1 === step ? "step" : undefined}
        >
          {i + 1}. {label}
        </li>
      ))}
    </ol>
  );
}

function RegisterWizard({
  initial,
  onClose,
  onDone,
}: {
  /** 从详情卡进入时跳过建档步，直接分配床位/设备 */
  initial: Pick<WizardState, "step" | "elderId" | "elderName">;
  onClose: () => void;
  onDone: () => void;
}) {
  const [state, setState] = useState<WizardState>({ ...initial, roomId: null, bedId: null, deviceId: null, secret: null });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // 步骤 1 表单
  const [name, setName] = useState("");
  const [birthDate, setBirthDate] = useState("");
  const [gender, setGender] = useState("");

  // 步骤 2 数据
  const [rooms, setRooms] = useState<RoomWithBeds[] | null>(null);
  const [selRoom, setSelRoom] = useState("");
  const [selBed, setSelBed] = useState("");

  // 步骤 3 数据
  const [devices, setDevices] = useState<DeviceItem[] | null>(null);
  const [selDevice, setSelDevice] = useState("");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<DeviceCreated | null>(null);

  useEffect(() => {
    if (state.step !== 2 || rooms) return;
    let cancelled = false;
    setBusy(true);
    fetchRooms()
      .then((items) => {
        if (!cancelled) setRooms(items);
      })
      .catch(() => {
        if (!cancelled) setError("房间床位加载失败，请稍后重试");
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [state.step, rooms]);

  useEffect(() => {
    if (state.step !== 3 || devices) return;
    let cancelled = false;
    setBusy(true);
    fetchDevices()
      .then((items) => {
        if (!cancelled) setDevices(items);
      })
      .catch(() => {
        if (!cancelled) setError("设备列表加载失败，请稍后重试");
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [state.step, devices]);

  async function submitElder() {
    if (!name.trim()) {
      setError("请填写长者姓名");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await createElder({
        name: name.trim(),
        ...(birthDate ? { birth_date: birthDate } : {}),
        ...(gender ? { gender } : {}),
      });
      if (res.status === 403) {
        setError("当前账号无注册权限，仅管理员与护工可操作");
        return;
      }
      if (!res.ok) {
        setError(res.status === 422 ? "请检查表单内容（姓名必填，日期格式 YYYY-MM-DD）" : "注册失败，请稍后重试");
        return;
      }
      const body = (await res.json()) as { elder_id: string };
      setState((s) => ({ ...s, step: 2, elderId: body.elder_id, elderName: name.trim() }));
    } catch {
      setError("网络异常，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  async function submitBed() {
    if (!selBed) {
      setError("请选择空闲床位");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await assignBed(state.elderId!, selBed);
      if (res.status === 409) {
        setError("该床位刚被占用，请重新选择");
        await fetchRooms().then(setRooms).catch(() => undefined);
        setSelBed("");
        return;
      }
      if (!res.ok) {
        setError(res.status === 403 ? "当前账号无分配权限" : "分配床位失败，请稍后重试");
        return;
      }
      const body = (await res.json()) as { room_id: string };
      setState((s) => ({ ...s, step: 3, roomId: body.room_id, bedId: selBed }));
    } catch {
      setError("网络异常，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  async function submitDevice() {
    setBusy(true);
    setError(null);
    try {
      let deviceId = selDevice;
      let secret: string | null = null;
      if (creating) {
        const createdBody = await createDevice(state.roomId!);
        deviceId = createdBody.device_id;
        secret = createdBody.device_secret;
        setCreated(createdBody);
      }
      const res = await bindDevice(deviceId, state.roomId!, state.elderId!);
      if (!res.ok) {
        setError(
          res.status === 403
            ? "当前账号无绑定权限"
            : res.status === 409
              ? "设备刚被绑定，请刷新后重选"
              : "绑定失败，请稍后重试",
        );
        if (creating) setDevices(null); // 新建设备已落库但未绑定，回列表重选
        return;
      }
      setState((s) => ({ ...s, step: 4, deviceId, secret }));
    } catch {
      setError("网络异常，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  function finish() {
    onDone();
    onClose();
  }

  return (
    <div className="dlg-mask" role="dialog" aria-modal="true" aria-label="长者注册">
      <div className="dlg-card wiz-card">
        {state.step < 4 && stepsBar(state.step)}
        <h2 className="dlg-title">
          {state.step === 1 && "注册长者"}
          {state.step === 2 && `为 ${state.elderName} 分配床位`}
          {state.step === 3 && `为 ${state.elderName} 分配设备`}
          {state.step === 4 && "注册完成"}
        </h2>

        {state.step === 1 && (
          <>
            <div className="field">
              <label className="field-label" htmlFor="wiz-name">姓名 *</label>
              <input
                id="wiz-name"
                className="field-input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="长者姓名"
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="wiz-birth">出生日期</label>
              <input
                id="wiz-birth"
                type="date"
                className="field-input"
                value={birthDate}
                onChange={(e) => setBirthDate(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="wiz-gender">性别</label>
              <select
                id="wiz-gender"
                className="field-input"
                value={gender}
                onChange={(e) => setGender(e.target.value)}
              >
                <option value="">不填</option>
                <option value="female">女</option>
                <option value="male">男</option>
              </select>
            </div>
          </>
        )}

        {state.step === 2 && (
          <>
            {!rooms && !error && <p className="wo-load-hint">正在加载房间床位…</p>}
            {rooms && (
              <>
                <div className="field">
                  <label className="field-label" htmlFor="wiz-room">房间</label>
                  <select
                    id="wiz-room"
                    className="field-input"
                    value={selRoom}
                    onChange={(e) => {
                      setSelRoom(e.target.value);
                      setSelBed("");
                    }}
                  >
                    <option value="">选择房间</option>
                    {rooms.map((room) => (
                      <option key={room.room_id} value={room.room_id}>
                        {room.room_no}
                      </option>
                    ))}
                  </select>
                </div>
                {selRoom && (
                  <ul className="wiz-beds">
                    {rooms
                      .find((room) => room.room_id === selRoom)!
                      .beds.map((bed) => {
                        const taken = bed.elder_id !== null;
                        return (
                          <li key={bed.bed_id}>
                            <label className={taken ? "wiz-bed wiz-bed-taken" : "wiz-bed"}>
                              <input
                                type="radio"
                                name="wiz-bed"
                                disabled={taken}
                                checked={selBed === bed.bed_id}
                                onChange={() => setSelBed(bed.bed_id)}
                              />
                              {bed.bed_no}
                              {taken && <span className="badge badge-neutral">已占用</span>}
                            </label>
                          </li>
                        );
                      })}
                    {rooms.find((room) => room.room_id === selRoom)!.beds.length === 0 && (
                      <li className="wo-empty">该房间暂无床位</li>
                    )}
                  </ul>
                )}
              </>
            )}
          </>
        )}

        {state.step === 3 && (
          <>
            <label className="wiz-bed">
              <input
                type="radio"
                name="wiz-device-mode"
                checked={!creating}
                onChange={() => setCreating(false)}
              />
              使用已有设备
            </label>
            {!creating && (
              <ul className="wiz-beds">
                {!devices && <li className="wo-load-hint">正在加载设备…</li>}
                {devices?.map((device) => {
                  const bound = device.elder_id !== null;
                  return (
                    <li key={device.device_id}>
                      <label className={bound ? "wiz-bed wiz-bed-taken" : "wiz-bed"}>
                        <input
                          type="radio"
                          name="wiz-device"
                          disabled={bound}
                          checked={selDevice === device.device_id}
                          onChange={() => setSelDevice(device.device_id)}
                        />
                        <span>
                          {device.room_no} · {device.device_id.slice(0, 8)}…
                        </span>
                        {bound && <span className="badge badge-neutral">已绑定</span>}
                      </label>
                    </li>
                  );
                })}
                {devices && devices.every((d) => d.elder_id !== null) && (
                  <li className="wo-empty">没有空闲设备，可选「新建设备」</li>
                )}
              </ul>
            )}
            <label className="wiz-bed">
              <input
                type="radio"
                name="wiz-device-mode"
                checked={creating}
                onChange={() => setCreating(true)}
              />
              新建设备（将生成一次性设备密钥）
            </label>
            {created && !creating && created.device_id === selDevice && (
              <p className="wiz-secret-note">密钥见下一步，关闭向导后不可再查看。</p>
            )}
          </>
        )}

        {state.step === 4 && (
          <div className="wiz-done">
            <p>长者 <strong>{state.elderName}</strong> 已完成建档、床位与设备绑定。</p>
            {state.secret && (
              <div className="wiz-secret">
                <p className="wiz-secret-title">设备密钥（仅显示这一次，请立即复制）</p>
                <code>{state.secret}</code>
              </div>
            )}
            <p className="wiz-done-hint">
              下一步：在平板设备页用
              <code> 设备 ID：{state.deviceId} </code>
              {state.secret ? "和上面的密钥" : "和建档时的密钥"}
              登录，登录成功后将自动进入声纹录入，按屏幕引导朗读三段短语即可。
            </p>
          </div>
        )}

        {error && <p className="field-error">{error}</p>}

        <div className="dlg-actions">
          {state.step < 4 ? (
            <>
              <button type="button" className="btn-secondary" onClick={onClose} disabled={busy}>
                取消
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={busy || (state.step === 1 && !name.trim())}
                onClick={() => {
                  if (state.step === 1) void submitElder();
                  else if (state.step === 2) void submitBed();
                  else void submitDevice();
                }}
              >
                {busy ? "处理中…" : state.step === 3 ? "绑定设备" : "下一步"}
              </button>
            </>
          ) : (
            <button type="button" className="btn-primary" onClick={finish}>
              完成
            </button>
          )}
        </div>
      </div>
    </div>
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
  const [wizard, setWizard] = useState<
    { step: number; elderId: string; elderName: string } | null
  >(null);
  const [refreshKey, setRefreshKey] = useState(0);

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
  }, [page, statusFilter, refreshKey]);

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const refreshList = useCallback(() => {
    setExpandedIds(new Set());
    setDetails({});
    setRefreshKey((k) => k + 1);
  }, []);

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
        <button
          type="button"
          className="btn-primary btn-sm"
          onClick={() => setWizard({ step: 1, elderId: "", elderName: "" })}
        >
          注册长者
        </button>
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
                    {details[e.elder_id] && (
                      <DetailCard
                        detail={details[e.elder_id]!}
                        onAssign={() =>
                          setWizard({ step: 2, elderId: e.elder_id, elderName: e.name })
                        }
                      />
                    )}
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

      {wizard && (
        <RegisterWizard
          initial={wizard}
          onClose={() => setWizard(null)}
          onDone={refreshList}
        />
      )}
    </div>
  );
}
