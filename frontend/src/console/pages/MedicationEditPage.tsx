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
 * 把用户常见的时间简写转成标准 5 段 cron（分 时 日 月 周）。
 * 已满足 cron 格式（含 4+ 段）→ 原样返回；
 * 不认识的输入 → 原样返回（交由后端严格校验兜底）。
 *
 * 支持的简写：
 *   "8:00"          → "0 8 * * *"       每天 08:00
 *   "08:30"         → "30 8 * * *"       每天 08:30
 *   "8:00, 20:00"   → "0 8,20 * * *"     每天 08:00 和 20:00
 *   "8:00,20:00"    → "0 8,20 * * *"     同上（去空格）
 */
function normalizeCron(raw: string): string {
  const s = raw.trim();
  if (!s) return s;

  // 已是标准 cron（包含空格分隔的 5 段）→ 原样返回
  if (/^\S+\s+\S+\s+\S+\s+\S+\s+\S+$/.test(s)) return s;

  // 匹配 "HH:MM, HH:MM[, ...]" 或 "HH:MM,HH:MM"
  const hhmmList = s
    .split(/[,\uff0c]/) // 中英文逗号
    .map((t) => t.trim())
    .filter(Boolean);

  if (hhmmList.length === 0) return s;

  const parts: Array<{ hour: string; minute: string }> = [];
  for (const t of hhmmList) {
    const m = t.match(/^(\d{1,2}):(\d{1,2})$/);
    if (!m) return s; // 任一子项不匹配 → 整体放弃转换，交给后端
    const h = parseInt(m[1], 10);
    const mi = parseInt(m[2], 10);
    if (h < 0 || h > 23 || mi < 0 || mi > 59) return s;
    parts.push({ hour: String(h), minute: String(mi) });
  }

  const minutes = parts.map((p) => p.minute).join(",");
  const hours = parts.map((p) => p.hour).join(",");
  return `${minutes} ${hours} * * *`;
}

/**
 * 把「每天多次」cron（分 列表 时 列表 * * *，如 "0 8,20 * * *"）解析成
 * HH:MM 列表，供时间选择器回填；不是该形态（如按周 cron）返回 null。
 */
function cronToTimes(cron: string): string[] | null {
  const m = cron.match(/^(\d{1,2}(?:,\d{1,2})*)\s+(\d{1,2}(?:,\d{1,2})*)\s+\*\s+\*\s+\*$/);
  if (!m) return null;
  const minutes = m[1].split(",");
  const hours = m[2].split(",");
  if (minutes.length !== hours.length || minutes.length === 0) return null;
  const times: string[] = [];
  for (let i = 0; i < hours.length; i++) {
    const h = Number(hours[i]);
    const mi = Number(minutes[i]);
    if (h > 23 || mi > 59) return null;
    times.push(`${String(h).padStart(2, "0")}:${String(mi).padStart(2, "0")}`);
  }
  return times;
}

/** 时间选择器值（HH:MM 列表）→ 标准 cron：["08:00","20:00"] → "0,0 8,20 * * *" */
function timesToCron(times: string[]): string {
  const hm = times.map((t) => t.split(":").map(Number));
  return `${hm.map((p) => p[1]).join(",")} ${hm.map((p) => p[0]).join(",")} * * *`;
}

/**
 * 药单维护（task-12 F8，console 医生，Mock 先行，与护工只读页共享 store）。
 *
 * 口径：§4.5 医生增改停查（无删除语义）；AC-27 修改 = version+1（历史提醒
 * 保留提交时快照）；服药时间默认时间选择器（多时间点 → 每天 cron），按周等
 * 复杂调度可切换 cron 文本录入；cronToText 展示转换（f8 D3）；
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
  // 服药时间默认用时间选择器；编辑既有药单且 cron 不是「每天 HH:MM」形态
  // （如按周调度）时退回 cron 文本编辑，避免丢信息
  const initialTimes = cronToTimes(initial.schedule_cron);
  const [useCron, setUseCron] = useState(initial.schedule_cron !== "" && initialTimes === null);
  const [times, setTimes] = useState<string[]>(initialTimes ?? ["08:00"]);
  const [cron, setCron] = useState(initial.schedule_cron);
  const [cronHint, setCronHint] = useState<string>("");
  const [startDate, setStartDate] = useState(initial.start_date);
  const [endDate, setEndDate] = useState(initial.end_date ?? "");
  const [error, setError] = useState<string | null>(null);

  /** 失焦时自动把常见时间简写转成标准 cron，并给用户反馈 */
  function handleCronBlur() {
    const normalized = normalizeCron(cron);
    if (normalized !== cron.trim()) {
      setCron(normalized);
      setCronHint(`已自动转成：${normalized}（${cronToText(normalized)}）`);
    } else {
      // 已经是标准 cron，给个预览
      if (/^\S+\s+\S+\s+\S+\s+\S+\s+\S+$/.test(normalized)) {
        setCronHint(`✓ ${cronToText(normalized)}`);
      } else {
        setCronHint("");
      }
    }
  }

  /** cron 文本 → 时间选择器：能解析则回填时间点，否则保留原时间列表 */
  function switchToTimes() {
    const parsed = cronToTimes(cron.trim());
    if (parsed && parsed.length > 0) setTimes(parsed);
    setUseCron(false);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    let cronValue: string;
    if (useCron) {
      if (!drugName.trim() || !dose.trim() || !cron.trim() || !startDate) {
        setError("药品名称、剂量、服药时间、开始日期均为必填");
        return;
      }
      cronValue = cron;
    } else {
      if (!drugName.trim() || !dose.trim() || times.length === 0 || times.some((t) => !t) || !startDate) {
        setError("药品名称、剂量、服药时间、开始日期均为必填");
        return;
      }
      cronValue = timesToCron(times);
    }
    onSubmit({
      elder_id: initial.elder_id,
      drug_name: drugName,
      dose,
      schedule_cron: cronValue,
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
      <div className="field">
        <span className="field-label">服药时间（必填）</span>
        {useCron ? (
          <>
            <input
              className="field-input"
              type="text"
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              onBlur={handleCronBlur}
              placeholder="如 0 8 * * *（每天 08:00）"
            />
            <small className="field-help">
              支持标准 cron：<code>0 8 * * *</code>（每天 08:00）、<code>0 8 * * 1</code>（每周一 08:00）。
              {cronHint && <span style={{ color: "#16a34a" }}>　{cronHint}</span>}
            </small>
            <button type="button" className="med-time-add" onClick={switchToTimes}>
              改用时间选择
            </button>
          </>
        ) : (
          <>
            {times.map((t, i) => (
              <div key={i} className="med-time-row">
                <input
                  className="field-input"
                  type="time"
                  value={t}
                  onChange={(e) => setTimes(times.map((x, j) => (j === i ? e.target.value : x)))}
                  aria-label={`第 ${i + 1} 个服药时间`}
                />
                {times.length > 1 && (
                  <button type="button" className="med-time-remove" onClick={() => setTimes(times.filter((_, j) => j !== i))}>
                    删除
                  </button>
                )}
              </div>
            ))}
            <div className="med-time-foot">
              <button type="button" className="med-time-add" onClick={() => setTimes([...times, "20:00"])}>
                ＋ 添加时间
              </button>
              <button type="button" className="med-time-add" onClick={() => setUseCron(true)}>
                改用 cron 表达式
              </button>
            </div>
            <small className="field-help">可添加多个时间点，每天按所选时间提醒：{times.filter(Boolean).join("、") || "（请选择时间）"}</small>
          </>
        )}
      </div>
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
