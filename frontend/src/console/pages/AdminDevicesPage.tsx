/**
 * 管理员 · 模拟设备（Day 5 · D-1 / N3）。
 *
 * 流程：房间床位列表（E18，复用 elders-api.fetchRooms）→ 设备建档（E16，复用 createDevice）
 * → 设备列表（E17，复用 fetchDevices）→ 一次性 device_secret 仅展示一次。
 *
 * 已知差异（只登记不修）：`elders-api.ts:createDevice` 非 2xx 时抛数字 status 而非 ApiError，
 * 本页用 typeof 类型守卫兜底，不改 elders-api.ts 签名。
 * 一次性密钥：只存组件 state，展示后关闭即丢弃；不写 localStorage、不写日志、不落任何持久化。
 */

import { useEffect, useState } from "react";
import { ApiError } from "../api";
import {
  createDevice,
  fetchDevices,
  fetchRooms,
  type DeviceCreated,
  type DeviceItem,
  type RoomWithBeds,
} from "../elders-api";

const ROOM_STATUS_LABELS: Record<string, string> = {
  available: "空闲",
  partial: "部分占用",
  full: "满员",
  maintenance: "维护中",
};

const DEVICE_STATUS_LABELS: Record<string, string> = {
  online: "在线",
  offline: "离线",
  locked: "锁定",
};

function createDeviceErrorText(status: number): string {
  switch (status) {
    case 403:
      return "当前账号无权建档设备";
    case 404:
      return "房间不存在，请刷新后重试";
    case 409:
      return "设备数据冲突，请刷新后重试";
    case 422:
      return "房间参数不合法，请重新选择";
    default:
      return "设备建档失败，请稍后重试";
  }
}

export function AdminDevicesPage() {
  const [rooms, setRooms] = useState<RoomWithBeds[] | null>(null);
  const [devices, setDevices] = useState<DeviceItem[] | null>(null);
  const [devicesError, setDevicesError] = useState<string | null>(null);
  const [selectedRoomId, setSelectedRoomId] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // 一次性密钥：仅此 state 持有；关闭后清空（不可再取）
  const [created, setCreated] = useState<DeviceCreated | null>(null);
  const [createdDeviceId, setCreatedDeviceId] = useState<string | null>(null);

  function loadDevices() {
    fetchDevices()
      .then((items) => {
        setDevices(items);
        setDevicesError(null);
      })
      .catch((err: unknown) => {
        setDevicesError(err instanceof ApiError ? err.message : "设备列表加载失败，请稍后重试");
      });
  }

  useEffect(() => {
    let cancelled = false;
    fetchRooms()
      .then((items) => {
        if (!cancelled) setRooms(items);
      })
      .catch(() => {
        if (!cancelled) setRooms([]);
      });
    loadDevices();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreateError(null);
    if (!selectedRoomId) {
      setCreateError("请先选择房间");
      return;
    }
    setCreating(true);
    try {
      const body = await createDevice(selectedRoomId);
      setCreated(body);
      setCreatedDeviceId(null);
      loadDevices();
    } catch (err) {
      if (typeof err === "number") {
        setCreateError(createDeviceErrorText(err));
      } else if (err instanceof ApiError) {
        setCreateError(err.message);
      } else {
        setCreateError("设备建档失败，请稍后重试");
      }
    } finally {
      setCreating(false);
    }
  }

  function closeSecret() {
    if (!created) return;
    // 关闭后丢弃明文，只保留设备 ID 供核对
    setCreatedDeviceId(created.device_id);
    setCreated(null);
  }

  return (
    <div className="admin-page">
      <section className="admin-card" aria-labelledby="admin-rooms-title">
        <h2 className="admin-card-title" id="admin-rooms-title">
          房间与床位
        </h2>
        <p className="admin-card-sub">对应 E18；床位占用以服务端返回的 elder_id 非空为准。</p>
        {rooms === null ? (
          <p className="admin-empty">房间床位加载中…</p>
        ) : rooms.length === 0 ? (
          <p className="admin-empty">暂无房间数据，请确认后端房间接口可用</p>
        ) : (
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">房间</th>
                <th scope="col">状态</th>
                <th scope="col">床位</th>
              </tr>
            </thead>
            <tbody>
              {rooms.map((room) => (
                <tr key={room.room_id}>
                  <td>{room.room_no}</td>
                  <td>{ROOM_STATUS_LABELS[room.status] ?? room.status}</td>
                  <td>
                    {room.beds
                      .map((bed) => `${bed.bed_no}（${bed.elder_id ? "已占用" : "空床"}）`)
                      .join("、")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="admin-card" aria-labelledby="admin-create-device-title">
        <h2 className="admin-card-title" id="admin-create-device-title">
          设备建档
        </h2>
        <p className="admin-card-sub">
          对应 E16；建档时必须选择房间，设备初始未绑定长者。响应中的设备密钥仅展示一次。
        </p>
        <form className="admin-form" onSubmit={handleCreate}>
          <div className="admin-field">
            <label htmlFor="device-room">房间</label>
            <select
              id="device-room"
              className="input"
              value={selectedRoomId}
              onChange={(e) => setSelectedRoomId(e.target.value)}
            >
              <option value="">请选择房间</option>
              {(rooms ?? []).map((room) => (
                <option key={room.room_id} value={room.room_id}>
                  {room.room_no}
                </option>
              ))}
            </select>
          </div>

          {createError && (
            <p className="alert-danger" role="alert">
              {createError}
            </p>
          )}

          {created && (
            <div className="admin-secret" role="status">
              <p className="admin-secret-title">设备密钥（仅本次展示）</p>
              <code className="admin-mono">{created.device_secret}</code>
              <p className="admin-secret-note">
                请立即录入平板。关闭后无法再次查看；本密钥不写入浏览器存储、不打印日志。
              </p>
              <div className="admin-actions">
                <button type="button" className="btn-secondary" onClick={closeSecret}>
                  我已记录，关闭
                </button>
              </div>
            </div>
          )}

          {!created && createdDeviceId && (
            <p className="admin-result" role="status">
              设备 <span className="admin-mono">{createdDeviceId}</span> 已建档；设备密钥已关闭，无法再次查看。
            </p>
          )}

          <div className="admin-actions">
            <button type="submit" className="btn-primary" disabled={creating || !selectedRoomId}>
              {creating ? "正在建档…" : "创建设备"}
            </button>
          </div>
        </form>
      </section>

      <section className="admin-card" aria-labelledby="admin-device-list-title">
        <h2 className="admin-card-title" id="admin-device-list-title">
          设备列表
        </h2>
        <p className="admin-card-sub">对应 E17；含未绑定设备，用于核对建档结果。</p>
        {devicesError && (
          <p className="alert-danger" role="alert">
            {devicesError}
          </p>
        )}
        {devices === null && !devicesError && <p className="admin-empty">设备列表加载中…</p>}
        {devices && devices.length === 0 && !devicesError && <p className="admin-empty">暂无设备</p>}
        {devices && devices.length > 0 && (
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">房间</th>
                <th scope="col">设备 ID</th>
                <th scope="col">长者绑定</th>
                <th scope="col">状态</th>
              </tr>
            </thead>
            <tbody>
              {devices.map((d) => (
                <tr key={d.device_id}>
                  <td>{d.room_no}</td>
                  <td className="admin-mono">{d.device_id}</td>
                  <td>{d.elder_id ? "已绑定" : "未绑定"}</td>
                  <td>{DEVICE_STATUS_LABELS[d.status] ?? d.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="admin-actions">
          <button type="button" className="btn-secondary" onClick={loadDevices}>
            刷新设备列表
          </button>
        </div>
      </section>
    </div>
  );
}
