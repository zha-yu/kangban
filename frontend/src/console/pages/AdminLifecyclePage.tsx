/**
 * 管理员 · 教学重置与退房（Day 9 · C6 · S5，节点 C6 三页之一）。
 *
 * 覆盖（任务卡 §6.2）：教学数据重置（FR-ADM-02）、长者退房（FR-ADM-09 → archived）、
 * away 暂离开关（E8 active/away）。三个操作均为**二次确认弹窗后才发请求**，
 * 取消则零请求（AC-19）；消费 A4 生命周期端点（lifecycle-api，路径集中常量，
 * A4 未交付时 404 → 页面「A4 未交付」提示）。
 *
 * 范围边界：退房后 pad/family 端到端表现（pad 401 / family 403）由 D 侧联调确认，
 * 本页只负责触发与 console 侧状态刷新（任务卡 §6.4）。
 */

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api";
import {
  checkoutElder,
  LifecycleApiError,
  setElderAway,
  teachingReset,
} from "../lifecycle-api";
import {
  ELDER_STATUS_LABELS,
  listElders,
  type ElderListItem,
  type ElderStatus,
} from "../elders-api";
import { ConfirmDialog } from "./ConfirmDialog";

function statusBadge(status: ElderStatus): string {
  switch (status) {
    case "active":
      return "badge badge-success";
    case "away":
      return "badge badge-warning";
    case "transferred":
      return "badge badge-info";
    case "archived":
      return "badge badge-neutral";
  }
}

function errorText(err: unknown): string {
  if (err instanceof LifecycleApiError || err instanceof ApiError) return err.message;
  return "操作失败，请稍后重试";
}

/** P2-25 同款：page_size=100 循环取满 total */
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

function elderLabel(e: ElderListItem): string {
  return e.room_no ? `${e.name}（${e.room_no} ${e.bed_no ?? ""}）` : `${e.name}（未入床）`;
}

/** 二次确认弹窗描述：确认后执行 action，成功信息写回页面 */
interface ConfirmState {
  title: string;
  sub?: string;
  body: string;
  confirmLabel: string;
  action: () => Promise<string>;
}

export function AdminLifecyclePage() {
  const [elders, setElders] = useState<ElderListItem[] | null>(null);
  const [eldersError, setEldersError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState("");

  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);

  const reloadElders = useCallback(() => {
    fetchAllElders()
      .then((items) => {
        setElders(items);
        setEldersError(null);
      })
      .catch(() => setEldersError("长者列表加载失败，请稍后重试"));
  }, []);

  useEffect(reloadElders, [reloadElders]);

  const selected = elders?.find((e) => e.elder_id === selectedId) ?? null;

  /** 弹窗确认：执行 → 成功信息展示；退房后刷新长者列表 */
  async function runConfirm() {
    if (!confirm) return;
    setConfirmBusy(true);
    setConfirmError(null);
    try {
      const msg = await confirm.action();
      setConfirm(null);
      setNotice({ ok: true, text: msg });
      reloadElders();
    } catch (err) {
      setConfirmError(errorText(err));
    } finally {
      setConfirmBusy(false);
    }
  }

  if (eldersError) {
    return (
      <div className="adm-page">
        <p className="wo-load-error">{eldersError}</p>
      </div>
    );
  }
  if (!elders) {
    return (
      <div className="adm-page">
        <p className="wo-load-hint">正在加载长者列表…</p>
      </div>
    );
  }

  return (
    <div className="adm-page">
      {notice && (
        <p className={notice.ok ? "adm-result" : "alert-danger"} role="status">
          {notice.text}
        </p>
      )}

      {/* ① 教学数据重置（FR-ADM-02） */}
      <section className="adm-section">
        <h2 className="adm-section-title">
          教学数据重置 <span className="badge badge-danger">危险</span>
        </h2>
        <p className="adm-note">
          清空教学演示期间产生的对话、工单、健康采集等数据并重置到初始状态。该操作不可撤销，请确认当前不在演示进行中。
        </p>
        <button
          type="button"
          className="btn-secondary"
          onClick={() => {
            setNotice(null);
            setConfirmError(null);
            setConfirm({
              title: "重置教学数据",
              body: "将清空教学演示数据并重置到初始状态，操作不可撤销。确定继续吗？",
              confirmLabel: "确认重置",
              action: async () => {
                await teachingReset();
                return "教学数据已重置";
              },
            });
          }}
        >
          重置教学数据…
        </button>
      </section>

      {/* ②③ 长者退房 / 暂离开关（操作对象共用一个长者选择） */}
      <section className="adm-section">
        <h2 className="adm-section-title">长者退房与暂离</h2>
        <p className="adm-note">
          退房后该长者转为 archived（FR-ADM-09）：pad 端会话失效、家属端不再可见（端到端表现由联调确认）；
          暂离为临时状态（away），可随时恢复。
        </p>
        <div className="adm-toolbar">
          <label className="field">
            <span className="field-label">选择长者</span>
            <select
              className="field-input"
              value={selectedId}
              onChange={(e) => {
                setSelectedId(e.target.value);
                setNotice(null);
              }}
            >
              <option value="">请选择…</option>
              {elders.map((e) => (
                <option key={e.elder_id} value={e.elder_id}>
                  {elderLabel(e)} · {ELDER_STATUS_LABELS[e.status]}
                </option>
              ))}
            </select>
          </label>
          {selected && (
            <span className={statusBadge(selected.status)}>
              {ELDER_STATUS_LABELS[selected.status]}
            </span>
          )}
        </div>

        <div className="adm-actions">
          <button
            type="button"
            className="btn-danger"
            disabled={!selected || selected.status === "archived"}
            onClick={() => {
              if (!selected) return;
              setNotice(null);
              setConfirmError(null);
              setConfirm({
                title: "办理退房",
                sub: elderLabel(selected),
                body: "退房后该长者转为已退房（archived），pad 端会话将失效、家属端不再可见。操作不可自动恢复，确定继续吗？",
                confirmLabel: "确认退房",
                action: async () => {
                  await checkoutElder(selected.elder_id);
                  setSelectedId("");
                  return `${selected.name} 已办理退房（archived）`;
                },
              });
            }}
          >
            办理退房…
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={!selected || selected.status === "archived"}
            onClick={() => {
              if (!selected) return;
              const goingAway = selected.status !== "away";
              setNotice(null);
              setConfirmError(null);
              setConfirm({
                title: goingAway ? "设置暂离" : "恢复在住",
                sub: elderLabel(selected),
                body: goingAway
                  ? "暂离期间数字人停止主动陪伴与健康采集，恢复在住后继续。确定继续吗？"
                  : "恢复后该长者回到在住（active）状态。确定继续吗？",
                confirmLabel: goingAway ? "确认暂离" : "确认恢复",
                action: async () => {
                  await setElderAway(selected.elder_id, goingAway);
                  return `${selected.name} 已${goingAway ? "设为暂离（away）" : "恢复在住（active）"}`;
                },
              });
            }}
          >
            {selected?.status === "away" ? "恢复在住…" : "设置暂离…"}
          </button>
        </div>
      </section>

      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          sub={confirm.sub}
          body={confirm.body}
          confirmLabel={confirm.confirmLabel}
          danger
          busy={confirmBusy}
          error={confirmError}
          onConfirm={() => void runConfirm()}
          onCancel={() => setConfirm(null)}
        />
      )}
    </div>
  );
}
