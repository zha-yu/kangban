import { useEffect, useState } from "react";
import { ApiError, USE_MOCK_API } from "../api";
import type { ElderListItem } from "../elders-api";
import {
  SOURCE_LABELS,
  submitHealthRecord,
  listResponsibleElders,
  type HealthRecordInput,
  type HealthRecordResult,
  type StaffRecordSource,
} from "../health-api";

/**
 * 健康采集代填表（task-09 F5，console，提交 Mock + 长者下拉真实 E7）。
 *
 * 口径：FR-HLT-06 / AC-32——5 字段代填，来源 staff_scheduled/staff_active；
 * 阈值常量按 PRD §4.6（红：收缩压≥180 或 舒张压≥120 单分量即红；黄档见 health-api.ts）；
 * 分级结果由 Mock 模拟、正式以后端规则引擎为准（f5 D3）；补录追加不覆盖（AC-22）。
 */

const MOOD_OPTIONS = [
  { value: 5, label: "5 · 很好" },
  { value: 4, label: "4 · 较好" },
  { value: 3, label: "3 · 一般" },
  { value: 2, label: "2 · 较差" },
  { value: 1, label: "1 · 很差" },
];

const SEVERITY_BADGE: Record<HealthRecordResult["severity"], string> = {
  red: "badge badge-danger",
  yellow: "badge badge-warning",
  green: "badge badge-success",
};

const SEVERITY_LABEL: Record<HealthRecordResult["severity"], string> = {
  red: "红档",
  yellow: "黄档",
  green: "绿档",
};

const TIME_FMT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

export function HealthFormPage() {
  const [elders, setElders] = useState<ElderListItem[] | null>(null);
  const [elderId, setElderId] = useState("");
  const [source, setSource] = useState<StaffRecordSource>("staff_scheduled");
  const [systolic, setSystolic] = useState("");
  const [diastolic, setDiastolic] = useState("");
  const [sleepHours, setSleepHours] = useState("");
  const [moodScore, setMoodScore] = useState<number | null>(null);
  const [moodNote, setMoodNote] = useState("");
  const [complaint, setComplaint] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<HealthRecordResult | null>(null);

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

  /** 口径校验（task-09 §2.3）；返回 null 表示通过 */
  function validate(): Omit<HealthRecordInput, "elder_id" | "source"> | null {
    const sys = systolic.trim() === "" ? null : Number(systolic);
    const dia = diastolic.trim() === "" ? null : Number(diastolic);
    const sleep = sleepHours.trim() === "" ? null : Number(sleepHours);

    for (const [name, v] of [
      ["收缩压", sys],
      ["舒张压", dia],
    ] as const) {
      if (v !== null && (!Number.isInteger(v) || v < 30 || v > 250)) {
        setError(`${name}需为 30–250 的整数 mmHg`);
        return null;
      }
    }
    if (sleep !== null && (!Number.isFinite(sleep) || sleep < 0 || sleep > 24)) {
      setError("睡眠时长需在 0–24 小时内（>24h 无效）");
      return null;
    }
    if (sys === null && dia === null && sleep === null && moodScore === null && !complaint.trim() && !moodNote.trim()) {
      setError("至少填写一项采集内容（缺失项可留空）");
      return null;
    }
    return {
      systolic: sys,
      diastolic: dia,
      sleep_hours: sleep,
      mood_score: moodScore,
      mood_note: moodNote.trim() || null,
      complaint: complaint.trim() || null,
    };
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const fields = validate();
    if (!fields || !elderId) return;
    setBusy(true);
    try {
      setResult(await submitHealthRecord({ elder_id: elderId, source, ...fields }));
    } catch {
      setError("提交失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  const selectedElder = elders?.find((e) => e.elder_id === elderId);

  return (
    <div className="hf-page">
      <p className="ntf-note">
        代填口径：数据来源记为护工代填（{SOURCE_LABELS[source]}），分级由规则引擎按 PRD §4.6 阈值写入并在日报标注来源；
        补录追加不覆盖历史记录。代填非负责长者的请求会被拒绝并记审计。
      </p>

      {error && <p className="field-error">{error}</p>}

      <form className="hf-card" onSubmit={handleSubmit}>
        <label className="field">
          <span className="field-label">长者（负责范围内）</span>
          <select className="field-input" value={elderId} onChange={(e) => setElderId(e.target.value)} required disabled={!elders}>
            {elders?.map((e) => (
              <option key={e.elder_id} value={e.elder_id}>
                {e.name}（{e.room_no ?? "—"} · {e.bed_no ?? "—"}）
              </option>
            ))}
          </select>
        </label>

        <fieldset className="hf-fieldset">
          <legend className="field-label">数据来源</legend>
          {(["staff_scheduled", "staff_active"] as const).map((s) => (
            <label key={s} className="hf-radio">
              <input
                type="radio"
                name="source"
                value={s}
                checked={source === s}
                onChange={() => setSource(s)}
              />
              {SOURCE_LABELS[s]}
            </label>
          ))}
        </fieldset>

        <div className="hf-grid">
          <label className="field">
            <span className="field-label">收缩压（mmHg，可空）</span>
            <input className="field-input" type="number" inputMode="numeric" min={30} max={250} value={systolic} onChange={(e) => setSystolic(e.target.value)} placeholder="如 135" />
          </label>
          <label className="field">
            <span className="field-label">舒张压（mmHg，可空）</span>
            <input className="field-input" type="number" inputMode="numeric" min={30} max={250} value={diastolic} onChange={(e) => setDiastolic(e.target.value)} placeholder="如 85" />
          </label>
          <label className="field">
            <span className="field-label">睡眠时长（小时，可空）</span>
            <input className="field-input" type="number" inputMode="decimal" min={0} max={24} step={0.5} value={sleepHours} onChange={(e) => setSleepHours(e.target.value)} placeholder="如 7.5" />
          </label>
          <label className="field">
            <span className="field-label">情绪自评（1–5，可空）</span>
            <select className="field-input" value={moodScore ?? ""} onChange={(e) => setMoodScore(e.target.value === "" ? null : Number(e.target.value))}>
              <option value="">未填写</option>
              {MOOD_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </label>
        </div>

        <label className="field">
          <span className="field-label">情绪备注（可空）</span>
          <input className="field-input" type="text" value={moodNote} onChange={(e) => setMoodNote(e.target.value)} placeholder="情绪自由描述" />
        </label>
        <label className="field">
          <span className="field-label">主诉（可空）</span>
          <input className="field-input" type="text" value={complaint} onChange={(e) => setComplaint(e.target.value)} placeholder="长者原话，如：昨晚没睡好" />
        </label>

        <div className="hf-actions">
          <button type="submit" className="btn-primary" disabled={busy || !elders || elders.length === 0}>
            {busy ? "提交中…" : "提交代填记录"}
          </button>
        </div>
      </form>

      {result && (
        <div className="hf-result">
          <div className="wo-badges">
            <span className={SEVERITY_BADGE[result.severity]}>{SEVERITY_LABEL[result.severity]}</span>
            <span className="badge badge-neutral">规则版本 {result.rule_version}</span>
          </div>
          <p className="hf-result-line">
            {selectedElder?.name ?? "长者"} · {SOURCE_LABELS[source]} ·{" "}
            {TIME_FMT.format(new Date(result.recorded_at))}
            {USE_MOCK_API ? "（Mock 提交，未落库）" : ""}
          </p>
          <p className="hf-result-followup">{result.follow_up}</p>
        </div>
      )}
    </div>
  );
}
