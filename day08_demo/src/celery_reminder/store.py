"""业务数据层 —— 用 SQLite 持久化「提醒」这个业务实体。

为什么 Redis 不够？
  - ReminderManager 投出去的"谁、提醒什么、什么时候"只藏在 RedBeat 条目的 args
    和 Redis 的 remind:once:{rid}→task_id 映射里，没有结构化存储
  - Redis 重启（哪怕 AOF 开了），业务侧想查"我的所有提醒"就没地方查
  - 按状态过滤（只看 pending 的）、统计、撤销恢复，都需要一个业务表

设计原则：
  - SQLite 零依赖（Python 标准库自带 sqlite3），演示方便
  - 表结构跟 DB 无关（schema 纯 ANSI SQL），生产换 Postgres 只需换 Driver
  - 调度状态（RedBeat/Worker 队列）仍然走 Redis；业务实体走 DB —— 两条线索并行
  - 创建顺序：**先写 DB，再投调度**（DB 失败就不投，保持一致）
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id          TEXT PRIMARY KEY,                  -- remind_id (UUID hex)
    agent_id    TEXT NOT NULL,
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('once', 'periodic')),
    schedule    TEXT NOT NULL,                     -- JSON: {seconds: 30} / {cron: '*/15 ...'} / {eta_iso: '...'}
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'firing', 'done', 'cancelled')),
    created_at  TEXT NOT NULL,                     -- ISO 8601 with timezone
    updated_at  TEXT NOT NULL,
    fired_at    TEXT                               -- 最近一次投递成功时间
);
CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status);
CREATE INDEX IF NOT EXISTS idx_reminders_agent  ON reminders(agent_id);
"""


class ReminderStore:
    """SQLite 业务表封装。线程安全（每线程一个连接）。"""

    def __init__(self, db_path: str | Path = "reminders.db"):
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._init_schema()

    # -------------------- 连接管理 --------------------

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                detect_types=sqlite3.PARSE_DECLTYPES,
                isolation_level=None,  # autocommit，每个操作自己 commit
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")   # 读写可并发
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    def _init_schema(self) -> None:
        # 建表可以用任意线程的连接
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    # -------------------- CRUD --------------------

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def insert(
        self,
        remind_id: str,
        agent_id: str,
        text: str,
        kind: str,
        schedule: dict,
        *,
        status: str = "pending",
    ) -> None:
        """创建一条提醒。已存在则 upsert（重启恢复场景复用同一 remind_id）。"""
        now = self._now_iso()
        self._conn.execute(
            """INSERT INTO reminders
               (id, agent_id, text, kind, schedule, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   agent_id=excluded.agent_id,
                   text=excluded.text,
                   kind=excluded.kind,
                   schedule=excluded.schedule,
                   status=excluded.status,
                   updated_at=excluded.updated_at""",
            (remind_id, agent_id, text, kind, json.dumps(schedule), status, now, now),
        )

    def update_status(
        self, remind_id: str, status: str, *, fired_at: bool = False
    ) -> None:
        """状态迁移。firing 时顺便记 fired_at。"""
        now = self._now_iso()
        if fired_at:
            self._conn.execute(
                """UPDATE reminders
                   SET status=?, fired_at=?, updated_at=?
                   WHERE id=?""",
                (status, now, now, remind_id),
            )
        else:
            self._conn.execute(
                """UPDATE reminders
                   SET status=?, updated_at=?
                   WHERE id=?""",
                (status, now, remind_id),
            )

    def get(self, remind_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM reminders WHERE id=?", (remind_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def list(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM reminders WHERE 1=1"
        params: list = []
        if agent_id:
            sql += " AND agent_id=?"
            params.append(agent_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete(self, remind_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM reminders WHERE id=?", (remind_id,))
        return cur.rowcount > 0

    # -------------------- 工具 --------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["schedule"] = json.loads(d["schedule"])
        return d

    def count(self, status: str | None = None) -> int:
        if status:
            return self._conn.execute(
                "SELECT COUNT(*) FROM reminders WHERE status=?", (status,)
            ).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]


# 模块级单例，默认存项目根目录的 reminders.db
store = ReminderStore()
