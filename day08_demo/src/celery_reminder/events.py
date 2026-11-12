"""Agent 侧事件消费骨架（讲义第 8.2 章）。

Celery Worker 只写事件流（agent:events），Agent 进程自己消费 —— 这样做的好处：
  - 提醒的处理发生在「用户真正在用的那个 Agent 进程里」，状态天然一致
  - Worker 与 Agent 可独立扩缩容
  - 消费侧有 ack 语义，处理失败可重投
"""

from __future__ import annotations

import json
import time

import redis

from celery_reminder.celery_app import app

rds = redis.Redis.from_url(
    app.conf.state_redis_url,
    decode_responses=True,
    # socket_timeout=None 让 xreadgroup 的 block 参数说了算
    # 否则默认 2s 就超时，比 block=5000ms 短，直接 TimeoutError
    socket_timeout=None,
    socket_connect_timeout=5,
    retry_on_timeout=True,
)
STREAM, GROUP = "agent:events", "agents"

# ---- 消费组初始化（幂等：已存在会抛 BUSYGROUP，忽略即可）----
try:
    rds.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
except redis.ResponseError:
    pass

# ---- Agent 消费侧幂等闸门（紧贴「给用户发消息」之前）----
AGENT_DONE_PREFIX = "agent:done:"


def guard_agent_action(remind_id: str) -> bool:
    """返回 True 表示首次看到这条提醒，可以执行副作用（发消息）。"""
    return bool(rds.set(f"{AGENT_DONE_PREFIX}{remind_id}", "1", nx=True, ex=86400))


def handle_event(event: dict) -> None:
    """真正的提醒动作 —— 这里只是打印，实际场景里是调 Agent 发消息。"""
    payload = event.get("payload", {})
    remind_id = payload.get("remind_id", "")
    agent_id = payload.get("agent_id", "unknown")
    text = payload.get("text", "")

    # 闸门 2：Agent 消费侧也做一次幂等
    if remind_id and not guard_agent_action(remind_id):
        print(f"[AGENT-SKIP] remind_id={remind_id} 已处理过，跳过")
        return

    # --- 这里是真正的提醒逻辑 ---
    print(f"[AGENT-WAKE] agent={agent_id}  remind_id={remind_id}  text={text}")
    time.sleep(0.01)  # 模拟 Agent 处理的一点点开销


def consume_once(consumer: str = "agent-demo", block_ms: int = 5000):
    """阻塞消费一次（拿到消息就处理，超时返回 None）。"""
    try:
        resp = rds.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=10, block=block_ms)
    except (redis.exceptions.TimeoutError, ConnectionError) as exc:
        print(f"[AGENT-RECONNECT] Redis 连接抖动，重连中…  ({exc})")
        rds.connection_pool.reset()
        return
    if not resp:
        return
    for _, messages in resp:
        for msg_id, fields in messages:
            event = json.loads(fields["data"])
            try:
                handle_event(event)
            except Exception as exc:
                print(f"[AGENT-ERROR] msg_id={msg_id}  error={exc}")
                continue
            rds.xack(STREAM, GROUP, msg_id)


def run_forever(consumer: str = "agent-demo"):
    """启动 Agent 消费循环（Ctrl+C 停止）。"""
    print(f"[AGENT] 开始消费事件流 stream={STREAM}  group={GROUP}  consumer={consumer}")
    print("[AGENT] 按 Ctrl+C 停止")
    try:
        while True:
            consume_once(consumer=consumer)
    except KeyboardInterrupt:
        print("\n[AGENT] 停止消费")


if __name__ == "__main__":
    run_forever()
