"""长者生命周期流水线（Day 9 A3，FR-ADM-02 教学重置 / FR-ADM-09 退房与 away）。

编排层定位（X12 契约，contracts.md:277-298）：

- **A 不直写他人表**：B/C 写域的业务数据清理一律经各域 service 的 purge 钩子；
  本模块只做**顺序编排、幂等去重、每步审计**。
- **审计由本流水线统一写，每步 1 条**；各域钩子不重复写 DB 审计。
- **每步独立可重入**：每步执行完即 commit，中途失败可单独重跑，**不回滚已吊销
  token**（X12 顺序硬口径）。幂等实现：执行前查 `audit_logs` 是否已有同
  `(elder_id, reason, step)` 的成功行，有则跳过（不写新行——审计行数恒等于步数）。
- **保留项**（FR-ADM-02）：账号 / 角色 / 配置 / 重置审计**不得清除**——各钩子的
  清理范围由契约冻结，本模块不做全表清理。
- **桩期（骨架先行）**：X4/X12 六钩子属 B/C 写域，签名冻结前以桩顶替，桩打
  `LIFECYCLE_STUB_MARKER` 标记且审计 detail 带 `stub=True`；联调段（D10 16:00）
  换真钩子后 grep 该标记须零命中。

自裁口径（登记计划 §8 执行记录）：

- Z-A4a：退房后 `elder.status='transferred'`（organization.py:87 枚举既有，账号
  与配置保留）；away 用 `'away'`，恢复回 `'active'`。
- Z-A4b：二次确认令牌 = 请求方显式传字面 `"CONFIRM"`，否则 `LifecycleConfirmError`
  （REST 层映射 409，零副作用）——不新增表、不引入新凭证体系。
- Z-A4c：桩统一返回 X12 口径 `PurgeRef(affected_rows, archived_rows, domain)`；
  X4 真钩子（`PurgeRef(deleted_chunks, deleted_vectors, audit_id)`，contracts.md:144）
  接入时在编排层做字段适配。
- Z-A4d：away 的**调度侧消费**（暂停采集与提醒触达、日报改暂离告知）归 B4 / C 的
  调度与日报逻辑读 `elder.status` 实现；本模块只做状态流转＋审计。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.runtime_context import Identity
from app.models import AuditLog, Elder
from app.services.audit_log_service import record_audit

logger = logging.getLogger("app.services.lifecycle_service")

#: 二次确认令牌字面值（Z-A4b；REST 层映射 409）。
#: 不是口令，是显式确认关键词——S105 误报，显式豁免留痕。
CONFIRM_TOKEN = "CONFIRM"  # noqa: S105

#: 桩标记（联调段换真钩子后 grep 本串须零命中）
LIFECYCLE_STUB_MARKER = "lifecycle-stub-x4-x12"

#: reason 两值（X12：contracts.md:292）
REASON_CHECKOUT = "checkout"
REASON_TEACHING_RESET = "teaching_reset"
REASON_AWAY = "away"

#: 退房流水线步骤顺序（X12 顺序硬口径：contracts.md:296）
CHECKOUT_STEPS: tuple[str, ...] = (
    "revoke_tokens",  # A 自有：设备 token_hash 轮换 + 家属绑定 unbound
    "purge_voiceprints",  # A 自有：voiceprint_service.delete_all_for_elder
    "purge_memory",  # X4（B）：memory_chunks 行 + 向量索引
    "archive_health",  # X12（C）：health_records 脱敏归档，不物理删
    "unbind_devices",  # A 自有：device_service.unbind_all_for_elder
)

#: 教学重置步骤顺序（业务数据清零，保留账号/角色/配置/重置审计）
RESET_STEPS: tuple[str, ...] = (
    "purge_memory",  # X4（B）
    "purge_medication",  # X12（B）：药单/事件/尝试
    "clear_health",  # X12（C）：健康记录清除（教学重置非归档）
    "purge_notifications",  # X12（C）
    "purge_reports",  # X12（C）
    "purge_work_orders",  # X12（C）：工单/流转/风险事件
)

_STEP_DOMAINS: dict[str, str] = {
    "purge_memory": "memory",
    "purge_medication": "medication",
    "archive_health": "health",
    "clear_health": "health",
    "purge_notifications": "notification",
    "purge_reports": "report",
    "purge_work_orders": "work_order",
}


# ------------------------------------------------------------------ 异常与结果


class LifecycleError(Exception):
    """生命周期流水线异常基类。"""


class LifecycleConfirmError(LifecycleError):
    """二次确认令牌缺失或不符：零副作用（REST 层映射 409）。"""


class LifecycleStepError(LifecycleError):
    """某一步失败：此前已成功的步骤保持（各自已 commit），重跑时按幂等跳过。"""

    def __init__(self, step: str, cause: Exception) -> None:
        self.step = step
        self.cause = cause
        super().__init__(f"lifecycle step {step!r} failed: {cause!r}")


class ElderNotFoundError(LifecycleError):
    """目标长者不存在。"""


@dataclass(frozen=True, slots=True)
class PurgeRef:
    """域清理钩子统一返回（X12 口径，contracts.md:292）。"""

    affected_rows: int
    archived_rows: int
    domain: str


@dataclass(slots=True)
class StepResult:
    step: str
    skipped: bool
    affected_rows: int = 0
    archived_rows: int = 0
    stub: bool = False


@dataclass(slots=True)
class LifecycleReport:
    elder_id: uuid.UUID
    reason: str
    success: bool
    steps: list[StepResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "elder_id": str(self.elder_id),
            "reason": self.reason,
            "success": self.success,
            "steps": [
                {
                    "step": s.step,
                    "skipped": s.skipped,
                    "affected_rows": s.affected_rows,
                    "archived_rows": s.archived_rows,
                    "stub": s.stub,
                }
                for s in self.steps
            ],
        }


# ------------------------------------------------------------------ 桩（X4/X12，联调段替换）


async def _stub_purge_hook(
    session: AsyncSession, *, elder_id: uuid.UUID, reason: str, domain: str
) -> PurgeRef:
    """B/C 写域钩子的桩：不触碰他人表，返回合成结果并带桩标记。

    真钩子签名（contracts.md v0.6）：
    `purge_elder_data(session, *, elder_id, reason) -> PurgeRef`——
    接入时删掉本函数、把 `_HOOKS` 里的项换成真实 service 函数即可，
    编排与审计零改动。
    """
    _ = session, elder_id, reason
    logger.warning("%s: stub hook used for domain=%s", LIFECYCLE_STUB_MARKER, domain)
    return PurgeRef(affected_rows=0, archived_rows=0, domain=domain)


#: 钩子注册表：step → 是否桩。联调段把 True 项换成真钩子并从本表移除桩标记。
_HOOKS: dict[str, tuple[str, bool]] = {
    "purge_memory": ("memory", True),  # X4（B）
    "purge_medication": ("medication", True),  # X12（B）
    "archive_health": ("health", True),  # X12（C）：退房=脱敏归档
    "clear_health": ("health", True),  # X12（C）：教学重置=清除
    "purge_notifications": ("notification", True),  # X12（C）
    "purge_reports": ("report", True),  # X12（C）
    "purge_work_orders": ("work_order", True),  # X12（C）
}


# ------------------------------------------------------------------ 幂等与审计


async def _step_already_done(
    session: AsyncSession, *, elder_id: uuid.UUID, reason: str, step: str
) -> bool:
    """幂等判定：audit_logs 已有同 (elder, reason, step) 的成功行即视为已完成。

    不写新 skip 行——审计行数恒等于实际执行的步数（任务书验收「审计行数=步数」）。
    """
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.action == f"lifecycle.{reason}.{step}",
            AuditLog.resource_id == elder_id,
            AuditLog.detail["reason"].as_string() == reason,
            AuditLog.detail["result"].as_string() == "success",
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _audit_step(
    session: AsyncSession,
    *,
    operator: Identity,
    elder_id: uuid.UUID,
    reason: str,
    step: str,
    affected_rows: int,
    archived_rows: int,
    stub: bool,
) -> None:
    """每步 1 条 DB 审计（X12：审计归 A，各域钩子不重复写）。"""
    await record_audit(
        session,
        actor_type="staff",
        actor_id=uuid.UUID(operator.staff_id) if operator.staff_id else None,
        action=f"lifecycle.{reason}.{step}",
        resource_type="elder",
        resource_id=elder_id,
        detail={
            "reason": reason,
            "step": step,
            "result": "success",
            "affected_rows": affected_rows,
            "archived_rows": archived_rows,
            "stub": stub,
        },
    )


# ------------------------------------------------------------------ A 自有步骤实现


async def _run_revoke_tokens(session: AsyncSession, *, elder_id: uuid.UUID) -> int:
    """步骤①：吊销该长者的全部凭证面。

    - 设备：token_hash 轮换 → 旧设备令牌下一次请求即 401（auth.py:292 前缀判定）；
    - 家属：`elder_guardians` active → unbound（解绑保痕，AC-24）→ 家属对该长者的
      范围授权失效（再访问详情 403）。家属自身账号与 token 不动（账号保留项）。

    放在 auth_service 的凭证域实现（X12「A 自有：auth_service.revoke_all_tokens」）。
    """
    from app.services.auth_service import revoke_all_tokens  # 局部导入避免环

    return await revoke_all_tokens(session, elder_id=elder_id)


async def _run_purge_voiceprints(
    session: AsyncSession, *, elder_id: uuid.UUID, operator: Identity
) -> int:
    """步骤②：删除该长者全部声纹样本与向量（Day 4 已有函数，薄复用）。"""
    from app.services.voiceprint_service import delete_all_for_elder

    return await delete_all_for_elder(session, elder_id, actor=f"staff:{operator.principal_id}")


async def _run_unbind_devices(session: AsyncSession, *, elder_id: uuid.UUID) -> int:
    """步骤⑤：解绑该长者名下全部设备（清绑定并置 offline；旧凭证已在步骤①失效）。"""
    from app.services.device_service import unbind_all_for_elder

    return await unbind_all_for_elder(session, elder_id=elder_id)


# ------------------------------------------------------------------ 流水线编排


async def _run_pipeline(
    session: AsyncSession,
    *,
    operator: Identity,
    elder_id: uuid.UUID,
    reason: str,
    steps: tuple[str, ...],
) -> LifecycleReport:
    """通用编排：幂等检查 → 执行 → 审计 → 每步 commit（X12 每步独立可重入）。"""
    report = LifecycleReport(elder_id=elder_id, reason=reason, success=True)

    for step in steps:
        if await _step_already_done(session, elder_id=elder_id, reason=reason, step=step):
            report.steps.append(StepResult(step=step, skipped=True))
            continue

        try:
            if step == "revoke_tokens":
                affected = await _run_revoke_tokens(session, elder_id=elder_id)
                archived, stub = 0, False
            elif step == "purge_voiceprints":
                affected = await _run_purge_voiceprints(
                    session, elder_id=elder_id, operator=operator
                )
                archived, stub = 0, False
            elif step == "unbind_devices":
                affected = await _run_unbind_devices(session, elder_id=elder_id)
                archived, stub = 0, False
            else:
                domain, is_stub = _HOOKS[step]
                if is_stub:
                    ref = await _stub_purge_hook(
                        session, elder_id=elder_id, reason=reason, domain=domain
                    )
                else:  # pragma: no cover - 联调段接入真钩子后走本分支
                    ref = await _call_real_hook(
                        session, step=step, elder_id=elder_id, reason=reason
                    )
                affected, archived, stub = ref.affected_rows, ref.archived_rows, is_stub
        except LifecycleStepError:
            raise
        except Exception as exc:
            await session.rollback()
            report.success = False
            raise LifecycleStepError(step, exc) from exc

        await _audit_step(
            session,
            operator=operator,
            elder_id=elder_id,
            reason=reason,
            step=step,
            affected_rows=affected,
            archived_rows=archived,
            stub=stub,
        )
        await session.commit()  # 每步独立落盘：中途失败不回滚已成功步骤
        report.steps.append(
            StepResult(
                step=step, skipped=False, affected_rows=affected, archived_rows=archived, stub=stub
            )
        )

    return report


async def _call_real_hook(
    session: AsyncSession, *, step: str, elder_id: uuid.UUID, reason: str
) -> PurgeRef:  # pragma: no cover - 联调段（D10 16:00）启用
    """真钩子调用入口（联调段实现字段适配，X4 的专有 PurgeRef 在此归一）。"""
    raise NotImplementedError("real hooks land in the integration phase")


def _require_confirm(confirm_token: str | None) -> None:
    if confirm_token != CONFIRM_TOKEN:
        raise LifecycleConfirmError("missing or invalid confirm token")


async def _load_elder(session: AsyncSession, elder_id: uuid.UUID) -> Elder:
    elder = await session.get(Elder, elder_id)
    if elder is None:
        raise ElderNotFoundError(str(elder_id))
    return elder


# ------------------------------------------------------------------ 对外入口


async def checkout_elder(
    session: AsyncSession,
    *,
    operator: Identity,
    elder_id: uuid.UUID,
    confirm_token: str | None,
) -> LifecycleReport:
    """退房流水线（FR-ADM-09）：五步清理 + 状态收口 transferred。"""
    _require_confirm(confirm_token)
    elder = await _load_elder(session, elder_id)

    report = await _run_pipeline(
        session, operator=operator, elder_id=elder_id, reason=REASON_CHECKOUT, steps=CHECKOUT_STEPS
    )

    elder.status = "transferred"
    await session.commit()
    return report


async def teaching_reset(
    session: AsyncSession,
    *,
    operator: Identity,
    elder_id: uuid.UUID,
    confirm_token: str | None,
) -> LifecycleReport:
    """教学周期重置（FR-ADM-02 / AC-19）：业务数据清零，账号/角色/配置/审计保留。"""
    _require_confirm(confirm_token)
    await _load_elder(session, elder_id)

    return await _run_pipeline(
        session,
        operator=operator,
        elder_id=elder_id,
        reason=REASON_TEACHING_RESET,
        steps=RESET_STEPS,
    )


async def set_away(
    session: AsyncSession,
    *,
    operator: Identity,
    elder_id: uuid.UUID,
    away: bool,
) -> LifecycleReport:
    """暂离/恢复（FR-ADM-09 后半）：只改状态 + 审计；采集与提醒的暂停由调度侧消费
    `elder.status` 实现（Z-A4d，交接 B4/C）。幂等：重复暂离不写第二条审计。
    """
    elder = await _load_elder(session, elder_id)
    target = "away" if away else "active"

    reason = REASON_AWAY
    step = "mark_away" if away else "resume"
    if elder.status == target or await _step_already_done(
        session, elder_id=elder_id, reason=reason, step=step
    ):
        return LifecycleReport(
            elder_id=elder_id,
            reason=reason,
            success=True,
            steps=[StepResult(step=step, skipped=True)],
        )

    elder.status = target
    await _audit_step(
        session,
        operator=operator,
        elder_id=elder_id,
        reason=reason,
        step=step,
        affected_rows=1,
        archived_rows=0,
        stub=False,
    )
    await session.commit()
    return LifecycleReport(
        elder_id=elder_id,
        reason=reason,
        success=True,
        steps=[StepResult(step=step, skipped=False, affected_rows=1)],
    )
