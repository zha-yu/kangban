"""Celery 任务定义 —— Worker 侧只负责「投递」，不做 Agent 的重逻辑。

分层职责（讲义第 8 章）：
  Beat        → 算时间、投递任务（不碰业务）
  Worker      → 执行 deliver_reminder：幂等判断 + 写事件流 + 回写业务状态
  Agent 进程  → 消费事件、发消息（在 Agent 自己的上下文里处理）
"""

from __future__ import annotations

import json
import time
import uuid

import redis

from celery_reminder.celery_app import app

# ---- 业务 Redis（db1：幂等键 / 撤销标记 / 事件流）----
_state_redis = redis.Redis.from_url(app.conf.state_redis_url, decode_responses=True)
EVENT_STREAM = "agent:events"

# ---- Agent 互斥锁（讲义 7.2 节：防止同一个 Agent 被并发唤醒）----
LOCK_PREFIX = "agent:lock:"
LOCK_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


@app.task(
    name="agent.reminder.deliver",
    ignore_result=True,
    acks_late=True,
    soft_time_limit=10,          # 抛 SoftTimeLimitExceeded，任务可 try/except 收尾
    time_limit=30,               # 硬杀
    autoretry_for=(ConnectionError, redis.exceptions.TimeoutError),
    retry_backoff=2,             # 2s, 4s, 8s ...
    retry_jitter=True,
    max_retries=5,
)
def deliver_reminder(payload: dict) -> None:
    """把一条提醒写进事件流 + 回写业务表状态。

    三重闸门：
      1. 软撤销（任务体第一行检查 cancel 标记）
      2. 幂等投递（SET NX 保证同一条提醒只投递一次）
      3. Agent 互斥锁（同一 Agent 不并发处理多条提醒）

    状态迁移（业务表）：
      创建时 pending → 闸门都过了 fired_at 记时间 → 投递成功
        - once    →  done （只触发一次）
        - periodic → 保持 firing （下一个周期 beat 再来，新的 remind_id）
    """
    # ---- 延迟 import store：Worker 进程 fork 后模块级 SQLite 连接会坏 ----
    from celery_reminder.store import store as db

    remind_id = payload.get("remind_id")
    agent_id = payload.get("agent_id")
    text = payload.get("text", "")
    kind = payload.get("kind", "once")   # once / periodic / unknown

    # ---- 自动补齐 remind_id（RedBeat 散装条目不传时兜底）----
    if not remind_id:
        remind_id = f"{agent_id or 'anon'}:{int(time.time()*1000)}:{text[:12]}"
        payload["remind_id"] = remind_id
        # 这种情况业务表里也可能没有记录，尝试补一条以防万一
        try:
            db.get(remind_id) or db.insert(
                remind_id=remind_id,
                agent_id=agent_id or "unknown",
                text=text,
                kind=kind,
                schedule={"auto": True},
            )
        except Exception as exc:
            print(f"[DB-FALLBACK] 补业务记录失败: {exc}")

    # ---- 闸门 1：软撤销（用户改主意了）----
    if _state_redis.exists(f"remind:cancel:{remind_id}"):
        print(f"[CANCELLED] remind_id={remind_id} 已被用户撤销，跳过投递")
        # 业务表同步改 cancelled
        try:
            db.update_status(remind_id, "cancelled")
        except Exception:
            pass
        return

    # ---- 闸门 2：幂等投递（at-least-once → exactly-once 的关键）----
    if not _state_redis.set(f"remind:sent:{remind_id}", "1", nx=True, ex=86400):
        print(f"[IDEMPOTENT] remind_id={remind_id} 已投递过，跳过")
        return

    # ---- 闸门 3：Agent 互斥锁 ----
    lock_key = None
    token = None
    if agent_id:
        lock_key = f"{LOCK_PREFIX}{agent_id}"
        token = uuid.uuid4().hex
        if not _state_redis.set(lock_key, token, nx=True, ex=15):
            print(f"[SKIP] agent_id={agent_id} 正在被其他提醒处理，本轮跳过（下一周期还会来）")
            return

    try:
        # ---- 先回写业务状态：firing ----
        try:
            db.update_status(remind_id, "firing", fired_at=True)
        except Exception as exc:
            print(f"[DB-WARN] 回写 firing 失败: {exc}")

        # ---- 写事件流（Redis Stream）----
        # 注意：Worker 只做这一件事，毫秒级完成
        _state_redis.xadd(
            EVENT_STREAM,
            {"data": json.dumps({"type": "reminder", "payload": payload}, ensure_ascii=False)},
            maxlen=100_000,
            approximate=True,
        )
        print(f"[DELIVERED] {payload}")

        # ---- 投递成功后：一次性提醒置 done ----
        if kind == "once":
            try:
                db.update_status(remind_id, "done")
            except Exception as exc:
                print(f"[DB-WARN] 回写 done 失败: {exc}")

    finally:
        if lock_key and token:
            _state_redis.eval(LOCK_RELEASE_LUA, 1, lock_key, token)
