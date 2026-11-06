import { useEffect, useState } from "react";
import { ApiError } from "../api";
import type { ElderListItem } from "../elders-api";
import {
  PLAN_STATUS_LABELS,
  cronToText,
  listPlansByElder,
  listResponsibleElders,
  type MedicationPlan,
} from "../medication-api";

/**
 * 药单只读（task-10 F6，console，Mock 药单 + 长者下拉真实 E7）。
 *
 * 口径：§4.5 有意决策——医生增改停查，护工/管理员只读；护工修改服务端 403 + 审计。
 * 页面零写操作（f6 D2）；version 仅展示（AC-27 快照语义在提示文案说明）；
 * 教学演示数据，不构成真实处方。
 */

const DATE_FMT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

function fmtDate(date: string): string {
  return DATE_FMT.format(new Date(date));
}

function PlanCard({ plan }: { plan: MedicationPlan }) {
  return (
    <li className={`med-card${plan.status === "stopped" ? " is-stopped" : ""}`}>
      <div className="wo-badges">
        <span className={`badge ${plan.status === "active" ? "badge-success" : "badge-neutral"}`}>
          {PLAN_STATUS_LABELS[plan.status]}
        </span>
        <span className="badge badge-neutral">v{plan.version}</span>
      </div>
      <div className="wo-title">
        <span className="wo-type">{plan.drug_name}</span>
        <span className="wo-elder">{plan.dose}</span>
      </div>
      <dl className="med-fields">
        <div><dt>服药时间</dt><dd>{cronToText(plan.schedule_cron)}</dd></div>
        <div><dt>开始日期</dt><dd>{fmtDate(plan.start_date)}</dd></div>
        <div><dt>结束日期</dt><dd>{plan.end_date ? fmtDate(plan.end_date) : "长期"}</dd></div>
        <div><dt>维护医生</dt><dd>{plan.created_by_name}</dd></div>
      </dl>
    </li>
  );
}

export function MedicationPage() {
  const [elders, setElders] = useState<ElderListItem[] | null>(null);
  const [elderId, setElderId] = useState("");
  const [plans, setPlans] = useState<MedicationPlan[] | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  useEffect(() => {
    if (!elderId) return;
    let cancelled = false;
    setPlans(null);
    setError(null);
    listPlansByElder(elderId)
      .then((items) => {
        if (!cancelled) setPlans(items);
      })
      .catch(() => {
        if (!cancelled) setError("药单加载失败，请稍后重试");
      });
    return () => {
      cancelled = true;
    };
  }, [elderId]);

  const selected = elders?.find((e) => e.elder_id === elderId);

  return (
    <div className="med-page">
      <p className="ntf-note">
        药单由医生在后台维护，护工仅可查看（修改请求将被拒绝并记审计）；每次提醒使用提交时的药品剂量快照，
        医生修改生成新版本、不影响历史提醒。本页数据为教学演示数据，不构成真实处方。
      </p>

      <div className="wo-filters" role="group" aria-label="长者选择">
        <label className="wo-filter">
          <span>长者</span>
          <select className="field-input" value={elderId} onChange={(e) => setElderId(e.target.value)} disabled={!elders}>
            {elders?.map((e) => (
              <option key={e.elder_id} value={e.elder_id}>
                {e.name}（{e.room_no ?? "—"} · {e.bed_no ?? "—"}）
              </option>
            ))}
          </select>
        </label>
        <span className="wo-count">{selected ? `${selected.name} 的用药计划` : ""}</span>
      </div>

      {error && <p className="wo-load-error">{error}</p>}
      {!plans && !error && elderId && <p className="wo-load-hint">正在加载药单…</p>}

      {plans && (
        <ul className="med-list">
          {plans.map((p) => (
            <PlanCard key={p.plan_id} plan={p} />
          ))}
          {plans.length === 0 && <li className="wo-empty">该长者当前没有用药计划</li>}
        </ul>
      )}
    </div>
  );
}
