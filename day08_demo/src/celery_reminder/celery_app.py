"""Celery 实例 + 全部配置（唯一事实来源）。

三个 Redis DB 各管一摊，互不干扰：
  db0 = Broker（任务消息）
  db1 = 业务状态（幂等键 / 撤销标记 / Agent 锁 / 事件流）
  db2 = RedBeat 定时表
"""

from celery import Celery

app = Celery("celery_reminder")

app.conf.update(
    # ---- Redis 连接 ----
    broker_url="redis://127.0.0.1:6379/0",                      # 任务消息队列
    state_redis_url="redis://127.0.0.1:6379/1",                  # 自定义项：业务状态
    redbeat_redis_url="redis://127.0.0.1:6379/2",                # RedBeat 定时表

    # ---- 序列化 & 安全 ----
    task_ignore_result=True,                                     # 提醒不需要返回值，省 result backend
    task_serializer="json",
    accept_content=["json"],

    # ---- 时区（重要！不配就是 UTC，crontab 会整体偏 8 小时）----
    timezone="Asia/Shanghai",
    enable_utc=True,

    # ---- 启动 & 模块 ----
    broker_connection_retry_on_startup=True,
    include=["celery_reminder.tasks"],

    # ---- RedBeat 配置 ----
    # 保持默认前缀，隔离靠上面的 DB，而不是改前缀
    redbeat_key_prefix="redbeat:",
    # 锁超时：不设则 = max_interval * 5；周期很长时可显式给
    redbeat_lock_timeout=60,
    # 动态增删的感知窗口：beat 最长要这么久才感知到新条目
    beat_max_loop_interval=5,
    # 直接在配置里指定 Scheduler，启动 beat 就不用每次敲 --scheduler
    beat_scheduler="redbeat.RedBeatScheduler",

    # ---- 队列分离 ----
    # 提醒任务独立队列，避免被其他重任务饿死
    task_default_queue="reminders",
    task_routes={"agent.reminder.*": {"queue": "reminders"}},

    # ---- 可靠性 ----
    # acks_late: Worker 执行完才 ack，保证 Worker 挂了任务不丢（代价：幂等要自己保证）
    task_acks_late=True,
    # prefetch=1: Worker 一次只取一个任务，避免取一堆然后被饿死
    worker_prefetch_multiplier=1,
)


if __name__ == "__main__":
    app.start()
