/**
 * 管理员 · 房间与床位管理（实施计划 v1.5 §3 / §6，E20–E28）。
 *
 * 页面结构：顶部筛选 → 房间表（房号/状态/床位数/占用数 + 编辑删除 + 分页）
 * → 床位表（房号/床号/状态/入住长者 + 复选框 + 编辑删除 + 分页）→ 批量条。
 * 房间状态由床位聚合、只读；床位 occupied 由入住关系派生，不提供手工切换。
 * 错误文案用用户语言，不回显 trace_id 与内部细节；样式只引用 tokens.css 变量。
 */

import { useEffect, useState } from "react";
import { AdminApiError } from "../admin-api";
import { ApiError } from "../api";
import {
  batchDeleteBeds,
  batchUpdateBedStatus,
  createAdminBed,
  createAdminRoom,
  deleteAdminBed,
  deleteAdminRoom,
  fetchAdminBeds,
  fetchAdminRooms,
  updateAdminBed,
  updateAdminRoom,
  type AdminBed,
  type AdminRoom,
  type BedManualStatus,
  type BedStatus,
  type RoomStatus,
} from "../admin-org-api";

const ROOM_PAGE_SIZE = 10;
const BED_PAGE_SIZE = 10;

const ROOM_STATUS_LABELS: Record<string, string> = {
  available: "空闲",
  partial: "部分占用",
  full: "满员",
  maintenance: "维护中",
};

const ROOM_STATUS_BADGE: Record<string, string> = {
  available: "badge-success",
  partial: "badge-info",
  full: "badge-warning",
  maintenance: "badge-neutral",
};

const BED_STATUS_LABELS: Record<string, string> = {
  available: "空闲",
  occupied: "占用",
  maintenance: "维修",
  disabled: "停用",
};

const BED_STATUS_BADGE: Record<string, string> = {
  available: "badge-success",
  occupied: "badge-warning",
  maintenance: "badge-neutral",
  disabled: "badge-neutral",
};

const MANUAL_STATUS_OPTIONS: Array<{ value: BedManualStatus; label: string }> = [
  { value: "available", label: "空闲" },
  { value: "maintenance", label: "维修" },
  { value: "disabled", label: "停用" },
];

interface Filters {
  roomNo: string;
  bedNo: string;
  roomStatus: "" | RoomStatus;
  bedStatus: "" | BedStatus;
}

function emptyFilters(): Filters {
  return { roomNo: "", bedNo: "", roomStatus: "", bedStatus: "" };
}

interface Feedback {
  ok: boolean;
  text: string;
  details?: string[];
}

interface DialogError {
  text: string;
  details?: string[];
}

type DeleteTarget =
  | { kind: "room"; room: AdminRoom }
  | { kind: "bed"; bed: AdminBed }
  | { kind: "batch"; ids: string[] };

function errorText(err: unknown): string {
  if (err instanceof AdminApiError || err instanceof ApiError) return err.message;
  return "操作失败，请稍后重试";
}

function elderText(bed: AdminBed): string {
  if (!bed.elder_id) return "空床";
  return bed.elder_name?.trim() ? bed.elder_name : `已入住（${bed.elder_id.slice(0, 8)}）`;
}

function roomBedCount(room: AdminRoom): number {
  return room.bed_count ?? room.beds?.length ?? 0;
}

function roomOccupiedCount(room: AdminRoom): number {
  return room.occupied_count ?? room.beds?.filter((bed) => bed.elder_id !== null).length ?? 0;
}

function Pager({
  page,
  pageSize,
  total,
  disabled,
  onChange,
}: {
  page: number;
  pageSize: number;
  total: number;
  disabled?: boolean;
  onChange: (page: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="admin-pager">
      <button
        type="button"
        className="btn-secondary btn-sm"
        disabled={disabled || page <= 1}
        onClick={() => onChange(page - 1)}
      >
        上一页
      </button>
      <span className="admin-pager-info">
        第 {page} / {totalPages} 页 · 共 {total} 条
      </span>
      <button
        type="button"
        className="btn-secondary btn-sm"
        disabled={disabled || page >= totalPages}
        onClick={() => onChange(page + 1)}
      >
        下一页
      </button>
    </div>
  );
}

export function AdminRoomsPage() {
  const [draft, setDraft] = useState<Filters>(emptyFilters);
  const [applied, setApplied] = useState<Filters>(emptyFilters);

  const [rooms, setRooms] = useState<AdminRoom[] | null>(null);
  const [roomTotal, setRoomTotal] = useState(0);
  const [roomPage, setRoomPage] = useState(1);
  const [roomsLoading, setRoomsLoading] = useState(false);
  const [roomsError, setRoomsError] = useState<string | null>(null);

  const [beds, setBeds] = useState<AdminBed[] | null>(null);
  const [bedTotal, setBedTotal] = useState(0);
  const [bedPage, setBedPage] = useState(1);
  const [bedsLoading, setBedsLoading] = useState(false);
  const [bedsError, setBedsError] = useState<string | null>(null);

  const [roomOptions, setRoomOptions] = useState<AdminRoom[]>([]);
  const [refreshKey, setRefreshKey] = useState(0);

  const [newRoomNo, setNewRoomNo] = useState("");
  const [newBedRoomId, setNewBedRoomId] = useState("");
  const [newBedNo, setNewBedNo] = useState("");
  const [roomBusy, setRoomBusy] = useState(false);
  const [bedBusy, setBedBusy] = useState(false);
  const [batchBusy, setBatchBusy] = useState(false);

  const [feedback, setFeedback] = useState<Feedback | null>(null);

  const [selected, setSelected] = useState<string[]>([]);

  const [editRoom, setEditRoom] = useState<AdminRoom | null>(null);
  const [editRoomNo, setEditRoomNo] = useState("");
  const [editBed, setEditBed] = useState<AdminBed | null>(null);
  const [editBedNo, setEditBedNo] = useState("");
  const [editBedStatus, setEditBedStatus] = useState<BedManualStatus>("available");
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [dialogBusy, setDialogBusy] = useState(false);
  const [dialogError, setDialogError] = useState<DialogError | null>(null);

  // 房间列表：随分页/已应用筛选/刷新计数重查
  useEffect(() => {
    let cancelled = false;
    setRoomsLoading(true);
    fetchAdminRooms({
      page: roomPage,
      pageSize: ROOM_PAGE_SIZE,
      roomNo: applied.roomNo,
      status: applied.roomStatus,
    })
      .then((res) => {
        if (cancelled) return;
        setRooms(res.items);
        setRoomTotal(res.total);
        setRoomsError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setRooms([]);
        setRoomTotal(0);
        setRoomsError(errorText(err));
      })
      .finally(() => {
        if (!cancelled) setRoomsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [roomPage, applied, refreshKey]);

  // 床位列表：同上，筛选使用床位号/床位状态
  useEffect(() => {
    let cancelled = false;
    setBedsLoading(true);
    fetchAdminBeds({
      page: bedPage,
      pageSize: BED_PAGE_SIZE,
      bedNo: applied.bedNo,
      status: applied.bedStatus,
    })
      .then((res) => {
        if (cancelled) return;
        setBeds(res.items);
        setBedTotal(res.total);
        setBedsError(null);
        // 刷新后剔除已不在当前页的选中项，避免批量作用于不可见床位
        setSelected((prev) => prev.filter((id) => res.items.some((bed) => bed.bed_id === id)));
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setBeds([]);
        setBedTotal(0);
        setBedsError(errorText(err));
        setSelected([]);
      })
      .finally(() => {
        if (!cancelled) setBedsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [bedPage, applied, refreshKey]);

  // 新建床位用的房间下拉：取全量前 100 间，不受上方筛选影响
  useEffect(() => {
    let cancelled = false;
    fetchAdminRooms({ page: 1, pageSize: 100 })
      .then((res) => {
        if (!cancelled) setRoomOptions(res.items);
      })
      .catch(() => {
        if (!cancelled) setRoomOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  function reload() {
    setRefreshKey((key) => key + 1);
  }

  function closeDialogs() {
    setEditRoom(null);
    setEditBed(null);
    setDeleteTarget(null);
    setDialogError(null);
  }

  function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    setApplied({
      roomNo: draft.roomNo.trim(),
      bedNo: draft.bedNo.trim(),
      roomStatus: draft.roomStatus,
      bedStatus: draft.bedStatus,
    });
    setRoomPage(1);
    setBedPage(1);
    setSelected([]);
    setFeedback(null);
  }

  function handleReset() {
    setDraft(emptyFilters());
    setApplied(emptyFilters());
    setRoomPage(1);
    setBedPage(1);
    setSelected([]);
    setFeedback(null);
  }

  async function handleCreateRoom(e: React.FormEvent) {
    e.preventDefault();
    setFeedback(null);
    const roomNo = newRoomNo.trim();
    if (!roomNo) {
      setFeedback({ ok: false, text: "请填写房号" });
      return;
    }
    setRoomBusy(true);
    try {
      await createAdminRoom(roomNo);
      setNewRoomNo("");
      setFeedback({ ok: true, text: `已新建房间 ${roomNo}` });
      reload();
    } catch (err) {
      setFeedback({ ok: false, text: errorText(err) });
    } finally {
      setRoomBusy(false);
    }
  }

  async function handleCreateBed(e: React.FormEvent) {
    e.preventDefault();
    setFeedback(null);
    if (!newBedRoomId) {
      setFeedback({ ok: false, text: "请选择所属房间" });
      return;
    }
    const bedNo = newBedNo.trim();
    if (!bedNo) {
      setFeedback({ ok: false, text: "请填写床号" });
      return;
    }
    setBedBusy(true);
    try {
      await createAdminBed(newBedRoomId, bedNo);
      setNewBedNo("");
      setFeedback({ ok: true, text: `已新建床位 ${bedNo}` });
      reload();
    } catch (err) {
      setFeedback({ ok: false, text: errorText(err) });
    } finally {
      setBedBusy(false);
    }
  }

  function openEditRoom(room: AdminRoom) {
    closeDialogs();
    setEditRoom(room);
    setEditRoomNo(room.room_no);
  }

  async function handleEditRoomSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!editRoom) return;
    const roomNo = editRoomNo.trim();
    if (!roomNo) {
      setDialogError({ text: "请填写房号" });
      return;
    }
    if (roomNo === editRoom.room_no) {
      closeDialogs();
      return;
    }
    setDialogBusy(true);
    setDialogError(null);
    try {
      await updateAdminRoom(editRoom.room_id, roomNo);
      setFeedback({ ok: true, text: `房号已更新为 ${roomNo}` });
      closeDialogs();
      reload();
    } catch (err) {
      setDialogError({ text: errorText(err) });
    } finally {
      setDialogBusy(false);
    }
  }

  function openEditBed(bed: AdminBed) {
    closeDialogs();
    setEditBed(bed);
    setEditBedNo(bed.bed_no);
    setEditBedStatus(bed.status === "occupied" ? "available" : bed.status);
  }

  async function handleEditBedSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!editBed) return;
    const bedNo = editBedNo.trim();
    if (!bedNo) {
      setDialogError({ text: "请填写床号" });
      return;
    }
    const occupied = editBed.elder_id !== null || editBed.status === "occupied";
    setDialogBusy(true);
    setDialogError(null);
    try {
      await updateAdminBed(editBed.bed_id, occupied ? { bedNo } : { bedNo, status: editBedStatus });
      setFeedback({ ok: true, text: `床位 ${editBed.room_no} ${bedNo} 已更新` });
      closeDialogs();
      reload();
    } catch (err) {
      setDialogError({ text: errorText(err) });
    } finally {
      setDialogBusy(false);
    }
  }

  function requestDeleteRoom(room: AdminRoom) {
    closeDialogs();
    setDeleteTarget({ kind: "room", room });
  }

  function requestDeleteBed(bed: AdminBed) {
    closeDialogs();
    setDeleteTarget({ kind: "bed", bed });
  }

  function requestBatchDelete() {
    if (!selected.length) return;
    closeDialogs();
    setDeleteTarget({ kind: "batch", ids: [...selected] });
  }

  /** 本地已知的“占用”床位明细，用于 409 提示；不解析后端 detail */
  function occupiedDetails(ids: string[]): string[] {
    return (beds ?? [])
      .filter((bed) => ids.includes(bed.bed_id) && (bed.status === "occupied" || bed.elder_id !== null))
      .map((bed) => `${bed.room_no} ${bed.bed_no}（${BED_STATUS_LABELS[bed.status] ?? bed.status}）`);
  }

  async function handleConfirmDelete() {
    if (!deleteTarget) return;
    setDialogBusy(true);
    setDialogError(null);
    try {
      if (deleteTarget.kind === "room") {
        await deleteAdminRoom(deleteTarget.room.room_id);
        setFeedback({ ok: true, text: `已删除房间 ${deleteTarget.room.room_no}` });
        if (rooms && rooms.length === 1 && roomPage > 1) setRoomPage(roomPage - 1);
      } else if (deleteTarget.kind === "bed") {
        await deleteAdminBed(deleteTarget.bed.bed_id);
        setFeedback({
          ok: true,
          text: `已删除床位 ${deleteTarget.bed.room_no} ${deleteTarget.bed.bed_no}`,
        });
        if (beds && beds.length === 1 && bedPage > 1) setBedPage(bedPage - 1);
      } else {
        const count = deleteTarget.ids.length;
        await batchDeleteBeds(deleteTarget.ids);
        setFeedback({ ok: true, text: `已批量删除 ${count} 个空床` });
        setSelected([]);
        if (beds && deleteTarget.ids.length >= beds.length && bedPage > 1) {
          setBedPage(bedPage - 1);
        }
      }
      closeDialogs();
      reload();
    } catch (err) {
      const conflict = err instanceof AdminApiError && err.kind === "conflict";
      const details =
        conflict && deleteTarget.kind === "batch" ? occupiedDetails(deleteTarget.ids) : [];
      setDialogError({ text: errorText(err), details });
    } finally {
      setDialogBusy(false);
    }
  }

  async function handleBatchStatus(status: BedManualStatus) {
    if (!selected.length) return;
    setFeedback(null);
    setBatchBusy(true);
    try {
      await batchUpdateBedStatus(selected, status);
      setFeedback({
        ok: true,
        text: `已将 ${selected.length} 个床位设为「${BED_STATUS_LABELS[status]}」`,
      });
      setSelected([]);
      reload();
    } catch (err) {
      const conflict = err instanceof AdminApiError && err.kind === "conflict";
      setFeedback({
        ok: false,
        text: errorText(err),
        details: conflict ? occupiedDetails(selected) : [],
      });
    } finally {
      setBatchBusy(false);
    }
  }

  function toggleBed(bedId: string) {
    setSelected((prev) =>
      prev.includes(bedId) ? prev.filter((id) => id !== bedId) : [...prev, bedId],
    );
  }

  const allSelected = Boolean(beds && beds.length > 0 && beds.every((bed) => selected.includes(bed.bed_id)));

  function toggleAllBeds() {
    if (!beds) return;
    setSelected(allSelected ? [] : beds.map((bed) => bed.bed_id));
  }

  return (
    <div className="admin-page">
      <section className="admin-card" aria-labelledby="admin-rooms-filter-title">
        <h2 className="admin-card-title" id="admin-rooms-filter-title">
          房间与床位管理
        </h2>
        <p className="admin-card-sub">
          房号与房间状态用于房间表；床位号与床位状态用于床位表。房间状态由床位聚合，不可手工修改。
        </p>
        <form className="admin-toolbar" onSubmit={handleSearch}>
          <div className="admin-field">
            <label htmlFor="filter-room-no">房号</label>
            <input
              id="filter-room-no"
              className="input"
              value={draft.roomNo}
              maxLength={32}
              placeholder="如 A-301"
              autoComplete="off"
              onChange={(e) => setDraft({ ...draft, roomNo: e.target.value })}
            />
          </div>
          <div className="admin-field">
            <label htmlFor="filter-bed-no">床位号</label>
            <input
              id="filter-bed-no"
              className="input"
              value={draft.bedNo}
              maxLength={32}
              placeholder="如 1 号床"
              autoComplete="off"
              onChange={(e) => setDraft({ ...draft, bedNo: e.target.value })}
            />
          </div>
          <div className="admin-field">
            <label htmlFor="filter-room-status">房间状态</label>
            <select
              id="filter-room-status"
              className="input"
              value={draft.roomStatus}
              onChange={(e) => setDraft({ ...draft, roomStatus: e.target.value as "" | RoomStatus })}
            >
              <option value="">全部</option>
              <option value="available">空闲</option>
              <option value="partial">部分占用</option>
              <option value="full">满员</option>
              <option value="maintenance">维护中</option>
            </select>
          </div>
          <div className="admin-field">
            <label htmlFor="filter-bed-status">床位状态</label>
            <select
              id="filter-bed-status"
              className="input"
              value={draft.bedStatus}
              onChange={(e) => setDraft({ ...draft, bedStatus: e.target.value as "" | BedStatus })}
            >
              <option value="">全部</option>
              <option value="available">空闲</option>
              <option value="occupied">占用</option>
              <option value="maintenance">维修</option>
              <option value="disabled">停用</option>
            </select>
          </div>
          <div className="admin-toolbar-actions">
            <button type="button" className="btn-secondary" onClick={handleReset}>
              重置
            </button>
            <button type="submit" className="btn-primary">
              查询
            </button>
          </div>
        </form>

        {feedback &&
          (feedback.ok ? (
            <p className="admin-result" role="status">
              {feedback.text}
            </p>
          ) : (
            <div className="alert-danger" role="alert">
              {feedback.text}
              {feedback.details && feedback.details.length > 0 && (
                <ul>
                  {feedback.details.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              )}
            </div>
          ))}
      </section>

      <section className="admin-card" aria-labelledby="admin-room-table-title">
        <h2 className="admin-card-title" id="admin-room-table-title">
          房间
        </h2>
        <p className="admin-card-sub">房号全局唯一；有床位或有设备的房间不能删除。</p>
        <form className="admin-form" onSubmit={handleCreateRoom}>
          <div className="admin-field">
            <label htmlFor="new-room-no">新建房间 · 房号</label>
            <input
              id="new-room-no"
              className="input"
              value={newRoomNo}
              maxLength={32}
              placeholder="如 A-303"
              autoComplete="off"
              onChange={(e) => setNewRoomNo(e.target.value)}
            />
          </div>
          <div className="admin-actions">
            <button type="submit" className="btn-primary" disabled={roomBusy}>
              {roomBusy ? "正在创建…" : "新建房间"}
            </button>
          </div>
        </form>

        {roomsError && (
          <p className="alert-danger" role="alert">
            {roomsError}
          </p>
        )}
        {rooms === null && roomsLoading && <p className="admin-empty">房间加载中…</p>}
        {rooms && rooms.length === 0 && !roomsError && <p className="admin-empty">暂无符合条件的房间</p>}
        {rooms && rooms.length > 0 && (
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">房号</th>
                <th scope="col">状态</th>
                <th scope="col">床位数</th>
                <th scope="col">占用数</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {rooms.map((room) => (
                <tr key={room.room_id}>
                  <td>{room.room_no}</td>
                  <td>
                    <span className={`badge ${ROOM_STATUS_BADGE[room.status] ?? "badge-neutral"}`}>
                      {ROOM_STATUS_LABELS[room.status] ?? room.status}
                    </span>
                  </td>
                  <td>{roomBedCount(room)}</td>
                  <td>{roomOccupiedCount(room)}</td>
                  <td>
                    <div className="admin-actions">
                      <button
                        type="button"
                        className="btn-secondary btn-sm"
                        onClick={() => openEditRoom(room)}
                      >
                        编辑
                      </button>
                      <button
                        type="button"
                        className="btn-ghost btn-sm"
                        onClick={() => requestDeleteRoom(room)}
                      >
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {rooms && rooms.length > 0 && (
          <Pager
            page={roomPage}
            pageSize={ROOM_PAGE_SIZE}
            total={roomTotal}
            disabled={roomsLoading}
            onChange={setRoomPage}
          />
        )}
      </section>

      <section className="admin-card" aria-labelledby="admin-bed-table-title">
        <h2 className="admin-card-title" id="admin-bed-table-title">
          床位
        </h2>
        <p className="admin-card-sub">
          床号在同一房间内唯一；占用由入住长者派生、不可手工设置；维修/停用仅限空床。
        </p>
        <form className="admin-form" onSubmit={handleCreateBed}>
          <div className="admin-field">
            <label htmlFor="new-bed-room">所属房间</label>
            <select
              id="new-bed-room"
              className="input"
              value={newBedRoomId}
              onChange={(e) => setNewBedRoomId(e.target.value)}
            >
              <option value="">请选择房间</option>
              {roomOptions.map((room) => (
                <option key={room.room_id} value={room.room_id}>
                  {room.room_no}
                </option>
              ))}
            </select>
          </div>
          <div className="admin-field">
            <label htmlFor="new-bed-no">床号</label>
            <input
              id="new-bed-no"
              className="input"
              value={newBedNo}
              maxLength={32}
              placeholder="如 2 号床"
              autoComplete="off"
              onChange={(e) => setNewBedNo(e.target.value)}
            />
          </div>
          <div className="admin-actions">
            <button type="submit" className="btn-primary" disabled={bedBusy}>
              {bedBusy ? "正在创建…" : "新建床位"}
            </button>
          </div>
        </form>

        {bedsError && (
          <p className="alert-danger" role="alert">
            {bedsError}
          </p>
        )}
        {beds === null && bedsLoading && <p className="admin-empty">床位加载中…</p>}
        {beds && beds.length === 0 && !bedsError && <p className="admin-empty">暂无符合条件的床位</p>}
        {beds && beds.length > 0 && (
          <>
            <table className="admin-table">
              <thead>
                <tr>
                  <th scope="col">
                    <input
                      type="checkbox"
                      aria-label="全选当前页床位"
                      checked={allSelected}
                      onChange={toggleAllBeds}
                    />
                  </th>
                  <th scope="col">房号</th>
                  <th scope="col">床号</th>
                  <th scope="col">状态</th>
                  <th scope="col">入住长者</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {beds.map((bed) => (
                  <tr key={bed.bed_id}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`选择 ${bed.room_no} ${bed.bed_no}`}
                        checked={selected.includes(bed.bed_id)}
                        onChange={() => toggleBed(bed.bed_id)}
                      />
                    </td>
                    <td>{bed.room_no}</td>
                    <td>{bed.bed_no}</td>
                    <td>
                      <span className={`badge ${BED_STATUS_BADGE[bed.status] ?? "badge-neutral"}`}>
                        {BED_STATUS_LABELS[bed.status] ?? bed.status}
                      </span>
                    </td>
                    <td>{elderText(bed)}</td>
                    <td>
                      <div className="admin-actions">
                        <button
                          type="button"
                          className="btn-secondary btn-sm"
                          onClick={() => openEditBed(bed)}
                        >
                          编辑
                        </button>
                        <button
                          type="button"
                          className="btn-ghost btn-sm"
                          onClick={() => requestDeleteBed(bed)}
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="admin-batch">
              <span className="admin-batch-count">已选 {selected.length} 个床位</span>
              <div className="admin-batch-actions">
                {MANUAL_STATUS_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    className="btn-secondary btn-sm"
                    disabled={!selected.length || batchBusy}
                    onClick={() => handleBatchStatus(opt.value)}
                  >
                    设为{opt.label}
                  </button>
                ))}
                <button
                  type="button"
                  className="btn-ghost btn-sm"
                  disabled={!selected.length || batchBusy}
                  onClick={requestBatchDelete}
                >
                  批量删空床
                </button>
              </div>
            </div>

            <Pager
              page={bedPage}
              pageSize={BED_PAGE_SIZE}
              total={bedTotal}
              disabled={bedsLoading || batchBusy}
              onChange={setBedPage}
            />
          </>
        )}
      </section>

      {editRoom && (
        <div className="dlg-mask">
          <div className="dlg-card" role="dialog" aria-modal="true" aria-labelledby="room-edit-title">
            <h2 className="dlg-title" id="room-edit-title">
              编辑房号
            </h2>
            <p className="dlg-sub">房间状态由床位聚合，不在此处修改。</p>
            <form onSubmit={handleEditRoomSubmit}>
              <div className="field">
                <label className="field-label" htmlFor="room-edit-no">
                  房号
                </label>
                <input
                  id="room-edit-no"
                  className="field-input"
                  value={editRoomNo}
                  maxLength={32}
                  autoComplete="off"
                  onChange={(e) => setEditRoomNo(e.target.value)}
                />
              </div>
              {dialogError && <p className="field-error">{dialogError.text}</p>}
              <div className="dlg-actions">
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={dialogBusy}
                  onClick={closeDialogs}
                >
                  取消
                </button>
                <button type="submit" className="btn-primary" disabled={dialogBusy}>
                  {dialogBusy ? "正在保存…" : "保存"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {editBed && (
        <div className="dlg-mask">
          <div className="dlg-card" role="dialog" aria-modal="true" aria-labelledby="bed-edit-title">
            <h2 className="dlg-title" id="bed-edit-title">
              编辑床位
            </h2>
            <p className="dlg-sub">{editBed.room_no} · 当前状态：{BED_STATUS_LABELS[editBed.status] ?? editBed.status}</p>
            <form onSubmit={handleEditBedSubmit}>
              <div className="field">
                <label className="field-label" htmlFor="bed-edit-no">
                  床号
                </label>
                <input
                  id="bed-edit-no"
                  className="field-input"
                  value={editBedNo}
                  maxLength={32}
                  autoComplete="off"
                  onChange={(e) => setEditBedNo(e.target.value)}
                />
              </div>
              <div className="field">
                <label className="field-label" htmlFor="bed-edit-status">
                  状态
                </label>
                {editBed.elder_id !== null || editBed.status === "occupied" ? (
                  <>
                    <select id="bed-edit-status" className="field-input" value="occupied" disabled>
                      <option value="occupied">占用</option>
                    </select>
                    <p className="admin-card-sub">该床位已入住长者，状态由入住关系决定，不可手工修改。</p>
                  </>
                ) : (
                  <select
                    id="bed-edit-status"
                    className="field-input"
                    value={editBedStatus}
                    onChange={(e) => setEditBedStatus(e.target.value as BedManualStatus)}
                  >
                    {MANUAL_STATUS_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                )}
              </div>
              {dialogError && <p className="field-error">{dialogError.text}</p>}
              <div className="dlg-actions">
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={dialogBusy}
                  onClick={closeDialogs}
                >
                  取消
                </button>
                <button type="submit" className="btn-primary" disabled={dialogBusy}>
                  {dialogBusy ? "正在保存…" : "保存"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {deleteTarget && (
        <div className="dlg-mask">
          <div className="dlg-card" role="dialog" aria-modal="true" aria-labelledby="delete-title">
            <h2 className="dlg-title" id="delete-title">
              {deleteTarget.kind === "room"
                ? "删除房间"
                : deleteTarget.kind === "bed"
                  ? "删除床位"
                  : "批量删除空床"}
            </h2>
            <p className="dlg-sub">
              {deleteTarget.kind === "room"
                ? `确认删除房间 ${deleteTarget.room.room_no}？该操作不可撤销。`
                : deleteTarget.kind === "bed"
                  ? `确认删除床位 ${deleteTarget.bed.room_no} ${deleteTarget.bed.bed_no}？该操作不可撤销。`
                  : `确认删除所选 ${deleteTarget.ids.length} 个空床？该操作不可撤销。`}
            </p>
            {dialogError && (
              <div className="alert-danger" role="alert">
                {dialogError.text}
                {dialogError.details && dialogError.details.length > 0 && (
                  <ul>
                    {dialogError.details.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            <div className="dlg-actions">
              <button
                type="button"
                className="btn-secondary"
                disabled={dialogBusy}
                onClick={closeDialogs}
              >
                取消
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={dialogBusy}
                onClick={handleConfirmDelete}
              >
                {dialogBusy ? "正在删除…" : "确认删除"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
