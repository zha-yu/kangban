"""三档身份级联自动化演示（H13④ · 服务级直调，真容器 + 真库 + 真阈值）。

按演示脚本三幕逐项执行并打印决策与库内留痕，证据直接可归档：

  幕1  无音频首轮 → source=device（设备锁定）
  幕2  本人音频（已注册声纹）→ source=voice，score ≥ 0.62
  幕3  第三者音频 → source=confirm/low_confidence（不落 thread）
  幕3b 冒充确认（confirmed≠候选集）→ source=generic（FR-VPR-03）

运行：
    uv run python scripts/cascade_demo_check.py
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from app.db import session_factory
from app.services import identity_service, voiceprint_service as vs

DEVICE_A = uuid.UUID("34b268ea-aece-4b4a-8017-d26c5c8285c0")  # 绑定 张桂芬
ELDER_A = uuid.UUID("11111111-1111-4111-8111-111111111102")  # 张桂芬
ELDER_IMPOSTOR = uuid.UUID("814f0d2b-72b0-4066-a947-ecf19874f341")  # 张三（非候选）
SAMPLES = Path(__file__).resolve().parent.parent / "spikes" / "voiceprint" / "samples"


def banner(title: str) -> None:
    print(f"\n{'=' * 12} {title} {'=' * 12}")


async def register_samples(session) -> None:
    await vs.delete_all_for_elder(session, elder_id=ELDER_A, actor="cascade-demo-reset")
    await session.commit()
    for idx in (1, 2, 3):
        raw = (SAMPLES / f"test1_{idx}.wav").read_bytes()
        status = await vs.register_sample(
            session=session, elder_id=ELDER_A, sample_index=idx, audio_bytes=raw  # type: ignore[arg-type]
        )
        await session.commit()
        print(f"  注册 test1_{idx}.wav → version={status.version} status={status.status}")


async def run() -> None:
    async with session_factory() as session:
        banner("准备：清理并注册 张桂芬 声纹（test1 三段）")
        await register_samples(session)

        banner("幕1 · 无音频首轮 → 期望 source=device")
        d1 = await identity_service.verify_session_identity(session, device_id=DEVICE_A, audio_bytes=None)
        await session.commit()
        print(f"  source={d1.source} elder={d1.elder_id} thread={d1.thread_id}")

        banner("幕2 · 本人音频 → 期望 source=voice，score ≥ 0.62")
        audio_a = (SAMPLES / "test1_3.wav").read_bytes()
        d2 = await identity_service.verify_session_identity(session, device_id=DEVICE_A, audio_bytes=audio_a)
        await session.commit()
        print(f"  source={d2.source} score={d2.confidence} elder={d2.elder_id} thread={d2.thread_id}")

        banner("幕3 · 第三者音频（test3）→ 期望 confirm/low_confidence，无 thread")
        audio_c = (SAMPLES / "test3_1.wav").read_bytes()
        d3 = await identity_service.verify_session_identity(session, device_id=DEVICE_A, audio_bytes=audio_c)
        await session.commit()
        print(f"  source={d3.source} reason={d3.reason} thread={d3.thread_id}")

        banner("幕3b · 冒充确认（张三 ≠ 候选集 张桂芬）→ 期望 generic")
        d4 = await identity_service.open_thread_from_confirm(
            session,
            device_id=DEVICE_A,
            confirmed_elder_id=ELDER_IMPOSTOR,
            candidate_elder_id=ELDER_A,
        )
        await session.commit()
        print(f"  source={d4.source} elder={d4.elder_id} thread={d4.thread_id}")

        banner("库内留痕：conversation_threads 最近 4 行")
        from sqlalchemy import select

        from app.models.conversation import ConversationThread  # noqa: PLC0415

        rows = (
            await session.execute(
                select(ConversationThread)
                .where(ConversationThread.device_id == DEVICE_A)
                .order_by(ConversationThread.started_at.desc())
                .limit(4)
            )
        ).scalars()
        for r in rows:
            print(
                f"  {r.started_at:%H:%M:%S} source={r.identity_source} "
                f"elder={str(r.elder_id)[:8] if r.elder_id else None} thread={str(r.thread_id)[:8]}…"
            )


if __name__ == "__main__":
    asyncio.run(run())
