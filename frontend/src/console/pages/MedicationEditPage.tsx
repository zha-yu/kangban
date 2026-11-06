import { useEffect, useState } from "react";
import { ApiError } from "../api";
import type { ElderListItem } from "../elders-api";
import {
  PLAN_STATUS_LABELS,
  cronToText,
  createPlan,
  listPlansByElder,
  listResponsibleElders,
  stopPlan,
  updatePlan,
  type MedicationPlan,
  type PlanWriteInput,
} from "../medication-api";

/**
 * 药单维护（task-12 F8，console 医生，Mock 先行，与护工只读页共享 store）。
 *
 * 口径：§4.5 医生增改停查（无删除语义）；AC-27 修改 = version+1（历史提醒
 * 保留提交时快照）；cron 文本录入 + cronToText 展示转换（f8 D3）；
 * 教学演示数据，不构成真实处方。
 */

const DOCTOR = "演示·钱医生"; // 真实接口后由登录态注入

const TIME_FMT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

function fmtDate(date: string): string {
  return TIME_FMT.format(new Date(date));
}

type DialogMode = { kind: "create" } | { kind: "update"; plan: MedicationPlan } | { kind: "stop"; plan: MedicationPlan } | null;

function PlanForm({
  initial,
  busy,
  onClose,
  onSubmit,
}: {
  initial: PlanWriteInput;
  busy: boolean;
  onClose: () => void;
  onSubmit: (input: PlanWriteInput) => void;
}) {
  const [drugName, setDrugName] = useState(initial.drug_name);
  const [dose, setDose] = useState(initial.dose);
  const [cron, setCron] = useState(initial.schedule_cron);
  const [startDate, setStartDate] = useState(initial.start_date);
  const [endDate, setEndDate] = useState(initial.end_date ?? "");
  const [error, setError] = useState<string | null>(null);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!drugName.trim() || !dose.trim() || !cron.trim() || !startDate) {
      setError("药品名称、剂量、服药时间、开始日期均为必填");
      return;
    }
    onSubmit({
      elder_id: initial.elder_id,
      drug_name: drugName,
      dose,
      schedule_cron: cron,
      start_date: startDate,
      end_date: endDate || null,
    });
  }

  return (
    <form className="rv-dialog" onClick={(e) => e.stopPropagation()} onSubmit={handleSubmit}>
      {error && <p className="field-error">{error}</p>}
      <label className="field">
        <span className="field-label">药品名称（必填）</span>
        <input className="field-input" type="text" value={drugName} onChange={(e) => setDrugName(e.target.value)} placeholder="如：硝苯地平缓释片" />
      </label>
      <label className="field">
        <span className="field-label">剂量（必填）</span>
        <input className="field-input" type="text" value={dose} onChange={(e) => setDose(e.target.value)} placeholder="如：20mg 每日一片" />
      </label>
      <label className="field">
        <span className="field-label">服药时间 cron（必填）</span>
        <input className="field-input" type="text" value={cron} onChange={(e) => setCron(e.target.value)} placeholder="如 0 8 * * *（每天 08:00）" />
      </label>
      <div className="hf-grid">
        <label className="field">
          <span className="field-label">开始日期（必填）</span>
          <input className="field-input" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label">结束日期（可空 = 长期）</span>
          <input className="field-input" type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
        </label>
      </div>
      <div className="hf-actions">
        <button type="button" className="btn-ghost" disabled={busy} onClick={onClose}>
          取消
        </button>
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? "提交中…" : "保存"}
        </button>
      </div>
    </form>
  );
}

export function MedicationEditPage() {
  const [elders, setElders] = useState<ElderListItem[] | null>(null);
  const [elderId, setElderId] = useState("");
  const [plans, setPlans] = useState<MedicationPlan[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogMode>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    listResponsibleElders()
      .then((items) => {
        setElders(items);
        if (items.length > 0) setElderId(items[0].elder_id);
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : "负责长者加载失败，请稍后重试");
      });
  }, []);

  function refresh(id: string) {
    listPlansByElder(id)
      .then((items) => {
        // active 前、stopped 后；同状态按 plan_id 倒序（新在前）
        setPlans(
          [...items].sort((a, b) => {
            if (a.status !== b.status) return a.status === "active" ? -1 : 1;
            return b.plan_id.localeCompare(a.plan_id);
          }),
        );
      })
      .catch(() => setError("药单加载失败，请稍后重试"));
  }

  useEffect(() => {
    if (elderId) refresh(elderId);
  }, [elderId]);

  async function handleSave(input: PlanWriteInput) {
    setBusy(true);
    try {
      if (dialog?.kind === "update") {
        await updatePlan(dialog.plan.plan_id, input, DOCTOR);
      } else {
        await createPlan({ ...input, elder_id: elderId }, DOCTOR);
      }
      setDialog(null);
      refresh(elderId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  async function handleStop(plan: MedicationPlan) {
    setBusy(true);
    try {
      await stopPlan(plan.plan_id, DOCTOR);
      setDialog(null);
      refresh(elderId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "停用失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  const selected = elders?.find((e) => e.elder_id === elderId);

  const initialInput: PlanWriteInput = {
    elder_id: elderId,
    drug_name: dialog?.kind === "update" ? dialog.plan.drug_name : "",
    dose: dialog?.kind === "update" ? dialog.plan.dose : "",
    schedule_cron: dialog?.kind === "update" ? dialog.plan.schedule_cron : "",
    start_date: dialog?.kind === "update" ? dialog.plan.start_date : new Date().toISOString().slice(0, 10),
    end_date: dialog?.kind === "update" ? (dialog.plan.end_date ?? "") : "",
  };

  return (
    <div className="med-page">
      <p className="ntf-note">
        药单由医生维护（增改停查，无删除语义）；修改生成新版本，历史提醒保留提交时的药品剂量快照不受影响。
        本页数据为教学演示数据，不构成真实处方。
      </p>

      {error && <p className="wo-load-error">{error}</p>}

      <div className="wo-filters" role="group" aria-label="长者选择与新增">
        <label className="wo-filter">
          <span>长者</span>
          <select className="field-input" value={elderId} onChange={(e) => setElderId(e.target.value)} disabled={!elders || !!dialog}>
            {elders?.map((e) => (
              <option key={e.elder_id} value={e.elder_id}>
                {e.name}（{e.room_no ?? "—"} · {e.bed_no ?? "—"}）
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn-primary" disabled={!elderId || !!dialog} onClick={() => setDialog({ kind: "create" })}>
          新增药单
        </button>
        <span className="wo-count">{selected ? `${selected.name} · ${plans?.length ?? 0} 条计划` : ""}</span>
      </div>

      <ul className="med-list">
        {plans?.map((p) => (
          <li key={p.plan_id} className={`med-card${p.status === "stopped" ? " is-stopped" : ""}`}>
            <div className="wo-badges">
              <span className={`badge ${p.status === "active" ? "badge-success" : "badge-neutral"}`}>
                {PLAN_STATUS_LABELS[p.status]}
              </span>
              <span className="badge badge-neutral">v{p.version}</span>
            </div>
            <div className="wo-title">
              <span className="wo-type">{p.drug_name}</span>
              <span className="wo-elder">{p.dose}</span>
            </div>
            <dl className="med-fields">
              <div><dt>服药时间</dt><dd>{cronToText(p.schedule_cron)}</dd></div>
              <div><dt>开始日期</dt><dd>{fmtDate(p.start_date)}</dd></div>
              <div><dt>结束日期</dt><dd>{p.end_date ? fmtDate(p.end_date) : "长期"}</dd></div>
              <div><dt>维护医生</dt><dd>{p.created_by_name}</dd></div>
            </dl>
            {p.status === "active" && (
              <div className="wo-actions">
                <button type="button" className="btn-ghost" disabled={!!dialog} onClick={() => setDialog({ kind: "update", plan: p })}>
                  修改
                </button>
                <button type="button" className="btn-ghost" disabled={!!dialog} onClick={() => setDialog({ kind: "stop", plan: p })}>
                  停用
                </button>
              </div>
            )}
          </li>
        ))}
        {plans && plans.length === 0 && <li className="wo-empty">该长者当前没有用药计划，可点击「新增药单」</li>}
      </ul>

      {dialog && (
        <div className="rv-mask" onClick={() => !busy && setDialog(null)}>
          {dialog.kind === "stop" ? (
            <div className="rv-dialog" onClick={(e) => e.stopPropagation()}>
              <h3 className="wo-dialog-title">停用确认</h3>
              <p className="ntf-note">
                确认停用「{dialog.plan.drug_name}（{dialog.plan.dose}）」？停用后不再生成提醒，记录保留可查、不可恢复；
                历史提醒保留提交时快照。
              </p>
              <div className="hf-actions">
                <button type="button" className="btn-ghost" disabled={busy} onClick={() => setDialog(null)}>
                  取消
                </button>
                <button type="button" className="btn-primary" disabled={busy} onClick={() => handleStop(dialog.plan)}>
                  {busy ? "处理中…" : "确认停用"}
                </button>
              </div>
            </div>
          ) : (
            <PlanForm
              key={dialog.kind === "update" ? dialog.plan.plan_id : "create"}
              initial={initialInput}
              busy={busy}
              onClose={() => setDialog(null)}
              onSubmit={handleSave}
            />
          )}
        </div>
      )}
    </div>
  );
}
