import { useEffect, useState } from "react";
import { getRecordChain, listPendingReviews, confirmReview, correctReview, type CorrectInput, type ReviewRecord } from "../review-api";
import { TrendChart } from "../TrendChart";

/**
 * 健康记录复核队列（task-11 F7，console 医生，Mock 先行）。
 *
 * 口径：FR-HLT-03/04 / AC-21/22——队列 = pending 黄/红档，红档置顶（f7 D2）；
 * 确认 → confirmed + 复核人留痕；纠错 → 新版本（version+1、severity 重算）+
 * 原记录 corrected + superseded_by 指向，原值保留可查（版本链）；
 * review_note 为 Mock 自裁字段（表无此列，f7 D4 留痕）。
 */

const SEVERITY_BADGE: Record<ReviewRecord["severity"], string> = {
  red: "badge badge-danger",
  yellow: "badge badge-warning",
  green: "badge badge-success",
};

const SEVERITY_LABEL: Record<ReviewRecord["severity"], string> = {
  red: "红档",
  yellow: "黄档",
  green: "绿档",
};

const SOURCE_LABEL: Record<ReviewRecord["source"], string> = {
  staff_scheduled: "定时代填",
  staff_active: "主动代填",
};

const TIME_FMT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

function fmtTime(iso: string): string {
  return TIME_FMT.format(new Date(iso));
}

function fmtValue(v: number | null, unit: string): string {
  return v === null ? "未填" : `${v} ${unit}`;
}

function RecordValues({ r }: { r: ReviewRecord }) {
  return (
    <dl className="med-fields">
      <div><dt>收缩压</dt><dd>{fmtValue(r.systolic, "mmHg")}</dd></div>
      <div><dt>舒张压</dt><dd>{fmtValue(r.diastolic, "mmHg")}</dd></div>
      <div><dt>睡眠</dt><dd>{r.sleep_hours === null ? "未填" : `${r.sleep_hours} h`}</dd></div>
      <div><dt>情绪</dt><dd>{r.mood_score === null ? "未填" : `${r.mood_score}/5${r.mood_note ? `（${r.mood_note}）` : ""}`}</dd></div>
      <div><dt>主诉</dt><dd>{r.complaint ?? "—"}</dd></div>
    </dl>
  );
}

/** 版本链展开：同批次全部版本，旧在前（AC-22 原值保留可查） */
function ChainView({ recordId, onClose }: { recordId: string; onClose: () => void }) {
  const [chain, setChain] = useState<ReviewRecord[] | null>(null);
  useEffect(() => {
    getRecordChain(recordId).then(setChain);
  }, [recordId]);

  return (
    <div className="rv-chain">
      <div className="rv-chain-head">
        <span className="wo-count">版本链（原始值保留，纠错不覆盖历史）</span>
        <button type="button" className="btn-ghost" onClick={onClose}>收起</button>
      </div>
      {chain?.map((r) => (
        <div key={r.record_id} className={`rv-chain-item${r.record_id === recordId ? " is-current" : ""}`}>
          <div className="wo-badges">
            <span className="badge badge-neutral">v{r.version}</span>
            <span className={SEVERITY_BADGE[r.severity]}>{SEVERITY_LABEL[r.severity]}</span>
            {r.superseded_reason === "review_correction" && <span className="badge badge-warning">已被纠错</span>}
            {r.record_id === recordId && <span className="badge badge-neutral">当前</span>}
          </div>
          <RecordValues r={r} />
          <p className="wo-history-item">
            {r.reviewed_by_name} 复核于 {r.reviewed_at ? fmtTime(r.reviewed_at) : "—"}
            {r.review_note ? ` · ${r.review_note}` : ""}
          </p>
        </div>
      ))}
    </div>
  );
}

export function ReviewQueuePage() {
  const [queue, setQueue] = useState<ReviewRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [chainId, setChainId] = useState<string | null>(null);
  // 趋势展开的长者（O1/FR-HLT-09：同一长者的多条记录共用一张趋势图）
  const [trendElderId, setTrendElderId] = useState<string | null>(null);
  // 纠错弹窗状态
  const [correcting, setCorrecting] = useState<ReviewRecord | null>(null);
  const [sys, setSys] = useState("");
  const [dia, setDia] = useState("");
  const [sleep, setSleep] = useState("");
  const [mood, setMood] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  // 演示复核人（真实接口后由登录态注入）
  const REVIEWER = "演示·钱医生";

  function refresh() {
    listPendingReviews()
      .then(setQueue)
      .catch(() => setError("复核队列加载失败，请稍后重试"));
  }

  useEffect(refresh, []);

  function openCorrect(r: ReviewRecord) {
    setCorrecting(r);
    setFormError(null);
    setSys(r.systolic === null ? "" : String(r.systolic));
    setDia(r.diastolic === null ? "" : String(r.diastolic));
    setSleep(r.sleep_hours === null ? "" : String(r.sleep_hours));
    setMood(r.mood_score);
    setNote("");
  }

  async function handleCorrect(e: React.FormEvent) {
    e.preventDefault();
    if (!correcting) return;
    // 至少改一项（f7 D3）
    const changed =
      (sys !== "" && Number(sys) !== correcting.systolic) ||
      (dia !== "" && Number(dia) !== correcting.diastolic) ||
      (sleep !== "" && Number(sleep) !== correcting.sleep_hours) ||
      (mood !== null && mood !== correcting.mood_score) ||
      note.trim() !== "";
    if (!changed) {
      setFormError("纠错至少需要修改一项数值或填写复核备注");
      return;
    }
    const fields: CorrectInput["fields"] = {};
    if (sys !== "" && Number(sys) !== correcting.systolic) fields.systolic = Number(sys);
    if (dia !== "" && Number(dia) !== correcting.diastolic) fields.diastolic = Number(dia);
    if (sleep !== "" && Number(sleep) !== correcting.sleep_hours) fields.sleep_hours = Number(sleep);
    if (mood !== null && mood !== correcting.mood_score) fields.mood_score = mood;
    const input: CorrectInput = { fields, review_note: note.trim() || null };
    setBusyId(correcting.record_id);
    try {
      await correctReview(correcting.record_id, input, REVIEWER);
      setCorrecting(null);
      setChainId(null);
      refresh();
    } catch (err) {
      setFormError(err instanceof Error && err.message !== "Failed to fetch" ? err.message : "纠错提交失败，请稍后重试");
    } finally {
      setBusyId(null);
    }
  }

  async function handleConfirm(r: ReviewRecord) {
    setBusyId(r.record_id);
    try {
      await confirmReview(r.record_id, REVIEWER, null);
      setChainId(null);
      refresh();
    } catch {
      setError("确认操作失败，请稍后重试");
    } finally {
      setBusyId(null);
    }
  }

  const pending = queue ?? [];
  const redCount = pending.filter((r) => r.severity === "red").length;

  return (
    <div className="rv-page">
      <p className="ntf-note">
        复核口径：黄档人工复核、红档紧急处理；确认无误或纠错后退出队列。纠错生成新版本并保留原始值可查（补录追加不覆盖）。
        复核备注为前端演示字段，正式落库方案待后端定稿。
      </p>

      {error && <p className="wo-load-error">{error}</p>}

      <div className="wo-filters">
        <span className="wo-count">待复核 {pending.length} 条{redCount > 0 ? ` · 红档 ${redCount} 条置顶` : ""}</span>
      </div>

      {!queue && !error && <p className="wo-load-hint">正在加载复核队列…</p>}
      {queue && pending.length === 0 && <p className="wo-empty">队列为空，所有黄/红档记录均已复核</p>}

      <ul className="med-list">
        {pending.map((r) => (
          <li key={r.record_id} className={`med-card${r.severity === "red" ? " rv-red" : ""}`}>
            <div className="wo-badges">
              <span className={SEVERITY_BADGE[r.severity]}>{SEVERITY_LABEL[r.severity]}</span>
              <span className="badge badge-neutral">v{r.version}</span>
              <span className="badge badge-neutral">{SOURCE_LABEL[r.source]}</span>
              <span className="badge badge-neutral">规则 {r.rule_version}</span>
            </div>
            <div className="wo-title">
              <span className="wo-type">{r.elder_name}</span>
              <span className="wo-elder">{r.room_no ?? "—"} 房 {r.bed_no ?? "—"} 床 · {fmtTime(r.recorded_at)} · {r.operator_name}</span>
            </div>
            <RecordValues r={r} />
            <div className="wo-actions">
              <button type="button" className="btn-primary" disabled={busyId === r.record_id} onClick={() => handleConfirm(r)}>
                确认无误
              </button>
              <button type="button" className="btn-ghost" disabled={busyId === r.record_id} onClick={() => openCorrect(r)}>
                纠错
              </button>
              {chainId === r.record_id ? (
                <button type="button" className="btn-ghost" onClick={() => setChainId(null)}>收起版本链</button>
              ) : (
                <button type="button" className="btn-ghost" onClick={() => setChainId(r.record_id)}>历史版本</button>
              )}
              {trendElderId === r.elder_id ? (
                <button type="button" className="btn-ghost" onClick={() => setTrendElderId(null)}>收起趋势</button>
              ) : (
                <button type="button" className="btn-ghost" onClick={() => setTrendElderId(r.elder_id)}>14 天趋势</button>
              )}
            </div>
            {chainId === r.record_id && <ChainView recordId={r.record_id} onClose={() => setChainId(null)} />}
            {trendElderId === r.elder_id && <TrendChart elderId={r.elder_id} elderName={r.elder_name} />}
          </li>
        ))}
      </ul>

      {correcting && (
        <div className="rv-mask" onClick={() => !busyId && setCorrecting(null)}>
          <form className="rv-dialog" onClick={(e) => e.stopPropagation()} onSubmit={handleCorrect}>
            <h3 className="wo-dialog-title">纠错 · {correcting.elder_name} v{correcting.version}</h3>
            <p className="ntf-note">修改将生成新版本（v{correcting.version + 1}），分级按阈值重算；原始值保留可查，不覆盖历史。</p>
            {formError && <p className="field-error">{formError}</p>}
            <div className="hf-grid">
              <label className="field">
                <span className="field-label">收缩压（mmHg）</span>
                <input className="field-input" type="number" inputMode="numeric" min={30} max={250} value={sys} onChange={(e) => setSys(e.target.value)} />
              </label>
              <label className="field">
                <span className="field-label">舒张压（mmHg）</span>
                <input className="field-input" type="number" inputMode="numeric" min={30} max={250} value={dia} onChange={(e) => setDia(e.target.value)} />
              </label>
              <label className="field">
                <span className="field-label">睡眠时长（小时）</span>
                <input className="field-input" type="number" inputMode="decimal" min={0} max={24} step={0.5} value={sleep} onChange={(e) => setSleep(e.target.value)} />
              </label>
              <label className="field">
                <span className="field-label">情绪自评（1–5）</span>
                <select className="field-input" value={mood ?? ""} onChange={(e) => setMood(e.target.value === "" ? null : Number(e.target.value))}>
                  <option value="">未填写</option>
                  {[5, 4, 3, 2, 1].map((v) => (
                    <option key={v} value={v}>{v}</option>
                  ))}
                </select>
              </label>
            </div>
            <label className="field">
              <span className="field-label">复核备注</span>
              <input className="field-input" type="text" value={note} onChange={(e) => setNote(e.target.value)} placeholder="纠错依据，如：现场二次测量" />
            </label>
            <div className="hf-actions">
              <button type="button" className="btn-ghost" disabled={busyId === correcting.record_id} onClick={() => setCorrecting(null)}>
                取消
              </button>
              <button type="submit" className="btn-primary" disabled={busyId === correcting.record_id}>
                {busyId === correcting.record_id ? "提交中…" : "提交纠错"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
