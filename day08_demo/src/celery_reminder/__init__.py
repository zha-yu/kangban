"""Celery + Redis 定时任务调度 —— Agent 提醒场景演示项目。

包名与讲义中的 ``agent_reminder`` 对应，仅因 uv 初始化包名惯例而叫 celery_reminder。
"""

from celery_reminder.celery_app import app
from celery_reminder.tasks import deliver_reminder

__all__ = ["app", "deliver_reminder"]
