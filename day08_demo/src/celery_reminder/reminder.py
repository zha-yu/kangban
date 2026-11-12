"""Agent 提醒的统一入口：一次性 / 定点 / 周期，全部走这里。

业务侧只调三个方法：
    reminder.once_in(payload, delay_seconds)
    reminder.once_at(payload, datetime带时区)
    reminder.every(payload, seconds=... 或 cron=crontab(...))

撤销、暂停、列表也各有对应方法。

--- 两条线索并行，保持一致 ---

  业务表（SQLite）         调度层（Redis）         业务层方法
  ──────────────────      ──────────────────      ──────────────────
  reminders.insert   ←─    apply_async / save  ←─ once_in / once_at / every
  reminders.cancel   ──→    revoke / delete     ──→ cancel_once / cancel_every
  reminders.list     ──→    (可选补充调度信息)  ──→ list_reminders
  reminders.update   ←─    Worker 投递后回写    ←─ tasks.py 里 update_status

创建顺序：**先写 DB，再投调度**。DB 失败就不投，保持一致。
"""

from __future__ import annotations

import uuid
from datetime import datetime

import redis
from celery.schedules import crontab
from redbeat import RedBeatSchedulerEntry

from celery_reminder.celery_app import app
from celery_reminder.store import ReminderStore, store
from celery_reminder.tasks import deliver_reminder

ONCE_KEY_PREFIX = "remind:once:"
PERIODIC_NAME_PREFIX = "remind-s-"


class ReminderManager:
    """一次性 + 周期提醒的统一管理器。"""

    def __init__(
        self,
        celery_app=None,
        db: ReminderStore | None = None,
    ):
        self.app = celery_app or app
        # 业务数据层（SQLite）
        self.db = db or store
        # 业务状态 Redis（db1）：撤销标记、幂等键 —— 这些是运行时的，不是业务持久化
        self.state = redis.Redis.from_url(
            self.app.conf.state_redis_url, decode_responses=True
        )
        # RedBeat 定时表 Redis（db2）：巡检/查询 RedBeat 条目
        self.sched = redis.Redis.from_url(
            self.app.conf.redbeat_redis_url, decode_responses=True
        )

    # ==================== 一次性提醒 ====================

    def once_in(
        self,
        payload: dict,
        delay_seconds: int,
        remind_id: str | None = None,
    ) -> str:
        """``delay_seconds`` 秒后提醒一次。"""
        remind_id = remind_id or uuid.uuid4().hex
        agent_id = payload.get("agent_id", "")
        text = payload.get("text", "")

        # ---- 1) 先写业务表 ----
        self.db.insert(
            remind_id=remind_id,
            agent_id=agent_id,
            text=text,
            kind="once",
            schedule={"seconds": int(delay_seconds)},
        )

        # ---- 2) 再投调度（DB 已成功，这里不会静默丢）----
        body = {**payload, "remind_id": remind_id, "kind": "once"}
        res = deliver_reminder.apply_async(
            args=[body], countdown=int(delay_seconds), queue="reminders"
        )
        self.state.setex(f"{ONCE_KEY_PREFIX}{remind_id}", 7 * 86400, res.id)
        return remind_id

    def once_at(
        self,
        payload: dict,
        at: datetime,
        remind_id: str | None = None,
    ) -> str:
        """在某个绝对时刻提醒一次。``at`` 必须带时区。"""
        if at.tzinfo is None:
            raise ValueError("at 必须带时区，例如 ZoneInfo('Asia/Shanghai')")
        remind_id = remind_id or uuid.uuid4().hex
        agent_id = payload.get("agent_id", "")
        text = payload.get("text", "")

        # ---- 1) 先写业务表 ----
        self.db.insert(
            remind_id=remind_id,
            agent_id=agent_id,
            text=text,
            kind="once",
            schedule={"eta_iso": at.isoformat()},
        )

        # ---- 2) 再投调度 ----
        body = {**payload, "remind_id": remind_id, "kind": "once"}
        res = deliver_reminder.apply_async(args=[body], eta=at, queue="reminders")
        self.state.setex(f"{ONCE_KEY_PREFIX}{remind_id}", 7 * 86400, res.id)
        return remind_id

    def cancel_once(self, remind_id: str) -> bool:
        """撤销尚未触发的一次性提醒。"""
        record = self.db.get(remind_id)
        if not record or record["status"] in ("done", "cancelled"):
            return False

        # ---- 1) 先改业务表 ----
        self.db.update_status(remind_id, "cancelled")

        # ---- 2) 再撤销调度（软撤销 + 广播撤销双保险）----
        self.state.set(f"remind:cancel:{remind_id}", "1", ex=86400)
        task_id = self.state.get(f"{ONCE_KEY_PREFIX}{remind_id}")
        if task_id:
            self.app.control.revoke(task_id, terminate=False)
            self.state.delete(f"{ONCE_KEY_PREFIX}{remind_id}")
        return True

    # ==================== 周期提醒（RedBeat）====================

    def every(
        self,
        payload: dict,
        seconds: int | None = None,
        cron: crontab | None = None,
        remind_id: str | None = None,
    ) -> str:
        """周期提醒。``seconds`` 固定间隔，``cron`` 日历式，二者必须且只能给一个。"""
        if (seconds is None) == (cron is None):
            raise ValueError("seconds 与 cron 必须且只能给一个")
        remind_id = remind_id or uuid.uuid4().hex
        agent_id = payload.get("agent_id", "")
        text = payload.get("text", "")

        # ---- 1) 先写业务表 ----
        schedule_payload: dict = (
            {"seconds": int(seconds)} if seconds is not None else {"cron": str(cron)}
        )
        self.db.insert(
            remind_id=remind_id,
            agent_id=agent_id,
            text=text,
            kind="periodic",
            schedule=schedule_payload,
        )

        # ---- 2) 再投调度 ----
        name = f"{PERIODIC_NAME_PREFIX}{remind_id}"
        entry = RedBeatSchedulerEntry(
            name=name,
            task=deliver_reminder.name,
            schedule=cron if cron is not None else int(seconds),
            args=({**payload, "remind_id": remind_id, "kind": "periodic"},),
            app=self.app,
        )
        entry.save()
        return remind_id

    def cancel_every(self, remind_id: str) -> bool:
        """删除一条周期提醒。"""
        record = self.db.get(remind_id)
        if not record or record["status"] == "cancelled":
            return False

        # ---- 1) 先改业务表 ----
        self.db.update_status(remind_id, "cancelled")

        # ---- 2) 再删调度 ----
        name = f"{PERIODIC_NAME_PREFIX}{remind_id}"
        key = RedBeatSchedulerEntry.generate_key(self.app, name)
        try:
            RedBeatSchedulerEntry.from_key(key, app=self.app).delete()
        except KeyError:
            pass
        return True

    def pause_every(self, remind_id: str, paused: bool = True) -> bool:
        """暂停但保留定义（``paused=False`` 即恢复）。"""
        name = f"{PERIODIC_NAME_PREFIX}{remind_id}"
        key = RedBeatSchedulerEntry.generate_key(self.app, name)
        try:
            entry = RedBeatSchedulerEntry.from_key(key, app=self.app)
        except KeyError:
            return False
        entry.enabled = not paused
        entry.save()
        # 暂停也记进业务表 schedule 里，恢复时可查
        record = self.db.get(remind_id)
        if record:
            schedule = record["schedule"]
            schedule["paused"] = paused
            self.db._conn.execute(  # 直接用底层（小操作，不值得开新 API）
                "UPDATE reminders SET schedule=?, updated_at=? WHERE id=?",
                (__import__("json").dumps(schedule), self.db._now_iso(), remind_id),
            )
        return True

    # ==================== 业务查询（从 DB 读，不再翻 RedBeat ZSET）====================

    def get(self, remind_id: str) -> dict | None:
        """按 remind_id 查一条。"""
        return self.db.get(remind_id)

    def list(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        """列出提醒。默认全部；可按 agent_id / status / kind 过滤。"""
        return self.db.list(agent_id=agent_id, status=status, kind=kind)

    def count(self, status: str | None = None) -> int:
        return self.db.count(status)

    # ---------- 兼容别名 ----------

    def list_every(self) -> list[dict]:
        """兼容旧 API —— 列出所有 periodic 提醒。"""
        return self.db.list(kind="periodic")


# 模块级单例，业务侧直接用 reminder.once_in(...) 即可
reminder = ReminderManager()
