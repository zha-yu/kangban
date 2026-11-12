# Celery + Redis 定时任务调度讲义

> **场景对齐**：用 Celery + Redis 做定时调度，调度结果用来**提醒 Agent**（唤醒 Agent、给 Agent 投递事件、触发 Agent 主动发消息/巡检查状态）。
>
> **技术栈校验**：Celery 5.7 / celery-redbeat 2.x / redis-py 5.x / Redis 6+ / Python 3.10+。
> 文中所有代码片段都按上述版本做过 **API 实测与语法编译校验**；涉及「必须有真实 Redis 才能观察」的行为，会在正文里单独标注（如 beat 感知动态任务的时间窗、ZSET 里的键名）。
>
> **本文已实测的四处关键行为**（都是最容易踩坑的地方，不是照抄文档）：
>
> 1. RedBeat 条目「新增 → 列表 → 暂停 → 删除 → 删除不存在抛 `KeyError`」全序列跑通；
> 2. `crontab(day_of_week=0)` 落在**周日**，`1` 才是周一；
> 3. `max_interval` 在 `beat_max_loop_interval=0` 时默认 **300 秒**，设为 5 后变成 5；
> 4. `RedBeatConfig` 只接受 `app` 一个参数，`key_prefix` 必须走 `app.conf.redbeat_key_prefix`。
>
> **这份讲义的立场**：先让你能把第一条提醒跑通，再一路推到「上千个 Agent、动态增删提醒、不重复、不丢、不并发打架」的生产形态。每一层都只解决上一层留下的具体问题。

***

## 目录

- 第 0 章 先分清四种「定时」
- 第 1 章 概念地图：谁在什么时间做什么
- 第 2 章 环境准备与最小骨架
- 第 3 章 第 1 层｜一次性延时提醒
- 第 4 章 第 2 层｜静态周期提醒（beat\_schedule）
- 第 5 章 第 3 层｜动态周期提醒（RedBeat）
- 第 6 章 第 4 层｜工程化封装（ReminderManager）
- 第 7 章 第 5 层｜可靠性与正确性
- 第 8 章 第 6 层｜架构：提醒任务只做投递
- 第 9 章 选型决策表 & 什么时候不该用 Celery
- 第 10 章 排错手册
- 第 11 章 自测题
- 附录 A 命令速查 / B 配置项速查 / C 术语表

***

# 第 0 章 先分清四种「定时」

这一章是全篇的地基。90% 的 Celery 定时问题，根因都是**把其中两种混成了同一种**。

| 语义       | 业务说法                | Celery 实现                                 | 需要 beat 吗 | 触发次数 |
| -------- | ------------------- | ----------------------------------------- | --------- | ---- |
| **延时一次** | 「30 分钟后提醒我」         | `apply_async(countdown=N)`                | 不需要       | 1 次  |
| **定点一次** | 「今天 15:30 提醒我」      | `apply_async(eta=带时区 datetime)`           | 不需要       | 1 次  |
| **固定间隔** | 「每 5 分钟检查一次」        | `beat_schedule` 里 `schedule=300`          | 需要        | 无限次  |
| **日历式**  | 「每天 9:00」「每周一 8:00」 | `beat_schedule` 里 `schedule=crontab(...)` | 需要        | 无限次  |

三条关键认知：

1. **`countdown`** **/** **`eta`** **不是「定时任务调度」，是「延迟投递」**。它只排队一次，没有周期，跟 beat 没有任何关系。很多人一开始就把它当定时器用，结果发现「不会重复执行」——因为它本来就不该重复。
2. **beat 是唯一的「闹钟」**。没有 beat 进程，任何周期性提醒都不会自己发生，Redis 也不会自己变出任务来。
3. **「定点」和「周期」不要互相代替**。要用「每天 9 点」这种语义，就必须是 `crontab`，不能用「每 86400 秒」凑——夏令时、重启漂移、闰秒都会让两者跑偏。

***

# 第 1 章 概念地图：谁在什么时间做什么

## 1.1 五个角色

| 角色                 | 是什么                 | 在提醒链路里干什么             | 挂了会怎样             |
| ------------------ | ------------------- | --------------------- | ----------------- |
| **Producer（业务进程）** | 你的 Agent 服务进程       | 产生提醒需求，把任务丢进 Broker   | 新提醒进不来，已排的照旧跑     |
| **Broker**         | 这里 = Redis          | 存待执行消息；延时消息先暂存到期再可见   | 全链路停摆             |
| **Beat（调度器）**      | `celery beat` 独立进程  | 到点把周期任务**投递**进 Broker | 周期提醒全停，一次性提醒不受影响  |
| **Worker**         | `celery worker` 进程池 | 从 Broker 取任务并**执行**   | 任务堆积在 Redis，不丢但要等 |
| **Agent 本体**       | 你的 Agent 主进程        | 消费提醒事件，真正「做提醒」这个动作    | 事件堆积，提醒延迟         |

**最容易搞混的一对**：Beat 只**发**任务，Worker 才**做**任务。Beat 死了不会「任务丢失」，只是不再有新任务被生成；Worker 死了任务还在 Redis 里等着。

## 1.2 一张数据流图

```
                      ┌───────────────────────────────┐
   周期提醒            │  Beat（celery beat 进程）      │
   （interval/cron）──▶│  ① 按定时表算出哪个任务到期     │
                      └──────────────┬────────────────┘
                                     │ ② 投递任务消息
   一次性提醒                         ▼
   （countdown/eta）───▶ ┌───────────────────────────┐
                        │  Redis（Broker）           │
                        │  ③ 消息排队；eta 消息先暂存 │
                        └──────────────┬────────────┘
                                       │ ④ 到期后消息对 Worker 可见
                                       ▼
                        ┌───────────────────────────┐
                        │  Worker（celery worker）   │
                        │  ⑤ 执行 deliver_reminder() │
                        └──────────────┬────────────┘
                                       │ ⑥ 只做投递：写事件流
                                       ▼
                        ┌───────────────────────────┐
                        │  Agent 主进程              │
                        │  ⑦ 消费事件 → 真正的提醒    │
                        └───────────────────────────┘
```

第 ⑥⑦ 步是这份讲义的核心主张，第 8 章会专门论证为什么要把 Worker 和 Agent 本体拆开。

## 1.3 一个反直觉的点：Celery 本身没有调度器

Celery 的库代码里没有「定时器」，`beat` 是**伴随组件**：它由 `celery beat` 命令启动，内部是一个死循环 `tick()` → 睡眠 → `tick()`。所以：

- `pip install celery` 装完并不能定时，**必须额外起一个 beat 进程**；
- 定时表存在哪里，由「Scheduler 类」决定，这才是第 4、5 章要换的东西。

Celery 自带的 Scheduler 有三类：

| Scheduler                        | 定时表存哪        | 动态增删     | 多 beat 去重 | 适合              |
| -------------------------------- | ------------ | -------- | --------- | --------------- |
| `celery.beat.Scheduler`（默认）      | **进程内存**     | 不行（重启即丢） | 无锁，会重复    | 纯静态、试玩          |
| `PersistentScheduler`（`-s` 指定文件） | 本地 shelve 文件 | 不行       | 无锁，会重复    | 单机静态            |
| `redbeat.RedBeatScheduler`       | **Redis**    | **可以**   | **有分布式锁** | Agent 场景（本讲义主线） |

## 1.4 Redis 在这里可能扮演三个角色，别混

| 角色                  | 配置项                     | 要不要               | 说明                                                 |
| ------------------- | ----------------------- | ----------------- | -------------------------------------------------- |
| Broker 消息中间件        | `broker_url`            | **必须**            | 任务消息队列，提醒链路的命脉                                     |
| Result Backend 结果存储 | `result_backend`        | **建议不要**          | 提醒任务不需要返回值，开了只会让 Redis 里堆一堆 `celery-task-meta-*` 键 |
| 业务状态存储              | `state_redis_url`（自定义项） | 强烈建议              | 幂等键、撤销标记、Agent 锁、提醒事件流，**与上面三者分库**                 |
| RedBeat 定时表存储       | `redbeat_redis_url`     | 用 RedBeat 时**必须** | 存定时定义、到期时间 ZSET、分布式锁                               |

> **实践建议**：提醒场景 `task_ignore_result=True` 且不配 backend。
> 如果只做提醒，Redis 里应该只有：任务消息 + RedBeat 的少量键 + 你自己的业务键。发现 `celery-task-meta-*` 变多，说明你误开了 result backend。

***

# 第 2 章 环境准备与最小骨架

## 2.1 安装

```bash
pip install "celery[redis]" redis celery-redbeat
```

- `celery-redbeat` 是 PyPI 包名，**导入名是** **`redbeat`**，这是新手第一个坑。
- 不需要 `django-celery-beat`，也不需要任何数据库。

## 2.2 Redis 自检

```bash
redis-cli -u redis://127.0.0.1:6379/0 ping      # 期望 PONG
redis-cli -u redis://127.0.0.1:6379/0 info server | head -3
```

生产环境额外确认两件事：

```bash
# 1) 持久化：至少 AOF everysec，否则 Redis 重启会丢排队中的提醒
redis-cli -u redis://127.0.0.1:6379/0 config get appendonly
# 2) 内存策略：必须是 noeviction，绝不能用 allkeys-lru
redis-cli -u redis://127.0.0.1:6379/0 config get maxmemory-policy
```

> **为什么** **`maxmemory-policy`** **必须是** **`noeviction`**：如果 Redis 内存满了开始 LRU 淘汰，它会**随机删掉队列里的消息**——表现就是「有些提醒莫名其妙没触发」，且日志里什么都没有。宁可写失败报错，也不要静默丢消息。

## 2.3 最小可用骨架

```
celery_reminder/
├── celery_app.py       # Celery 实例 + 全部配置（唯一入口）
└── tasks.py            # 任务定义
```

`celery_app.py`：

```python
from celery import Celery

app = Celery("celery_reminder")

app.conf.update(
    broker_url="redis://127.0.0.1:6379/0",
    # 提醒任务不关心返回值，省掉 result backend 与结果键
    task_ignore_result=True,
    # 序列化统一 json，杜绝 pickle 的安全隐患
    task_serializer="json",
    accept_content=["json"],
    # 时区：不配就是 UTC，crontab 会整体偏 8 小时
    timezone="Asia/Shanghai",
    enable_utc=True,
    # 启动时 Broker 不可用则重试，而不是直接崩
    broker_connection_retry_on_startup=True,
    # 任务模块：Worker 必须能 import 到，否则报 NotRegistered
    include=["celery_reminder.tasks"],
)

if __name__ == "__main__":
    app.start()
```

`tasks.py`：

```python
from celery_reminder.celery_app import app


@app.task(name="agent.reminder.deliver", ignore_result=True)
def deliver_reminder(payload: dict) -> None:
    """提醒任务：只负责把提醒投递出去，不在这里跑 Agent 的重逻辑。"""
    print(f"[REMIND] {payload}")
```

启动 Worker 验证骨架通畅：

```bash
celery -A celery_reminder.celery_app:app worker -l INFO
```

> `-A` 指向「能取到 app 对象的模块路径」，写 `celery_reminder.celery_app:app` 最不容易歧义。若模块里叫 `celery` 或 `app` 且是模块级变量，只写模块路径也行。

***

# 第 3 章 第 1 层｜一次性延时提醒

这一层**不需要 beat**。先把「往 Redis 丢一个任务、Worker 到点执行」这条线跑通，后面的周期性只是「谁在什么时候帮我重复丢」。

## 3.1 countdown：N 秒后提醒一次

```python
from celery_reminder.tasks import deliver_reminder

# 3 分钟后提醒 agent-001
deliver_reminder.apply_async(
    args=[{"agent_id": "agent-001", "text": "用户说 20 分钟后回来，该主动问一句了"}],
    countdown=180,
)
```

执行链路：消息立刻进 Redis，但**对 Worker 不可见**，直到 180 秒后才被搬进可消费队列。这意味着：

- 这 180 秒里 Worker 不用空转轮询，Redis 帮你「把闹钟挂在那儿」；
- 期间 Redis 重启且没开持久化 → 提醒直接消失（这就是 2.2 里强调 AOF 的原因）。

## 3.2 eta：指定某个时刻提醒一次

```python
from datetime import datetime
from zoneinfo import ZoneInfo

at = datetime(2026, 11, 11, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

deliver_reminder.apply_async(
    args=[{"agent_id": "agent-001", "text": "15:30 的评审会要开始了"}],
    eta=at,
)
```

**`eta`** **三个必须记住的点**：

1. `eta` 必须**带时区（aware）**。传 naive datetime 时 Celery 按 UTC 解释，你会得到「整体晚 8 小时」这个经典 bug。
2. `eta` 是**绝对时刻**，不受 `timezone` 配置影响（`timezone` 只影响 crontab 的解释）。所以同一句 `datetime(..., tzinfo=ZoneInfo("Asia/Shanghai"))` 在任何部署机器上都指向同一瞬间。
3. `eta` 已经过去时，Celery **立刻执行**（不会报错、不会跳过）。做「补发」逻辑时注意这一点。

## 3.3 怎么撤销一条还没触发的提醒

```python
res = deliver_reminder.apply_async(args=[payload], countdown=180)
task_id = res.id          # 记下来，存到业务侧（Redis / 表）

# 用户改主意了
app.control.revoke(task_id, terminate=False)
```

边界必须清楚：

- `revoke` 是一个**广播**动作，只对「还在队列里、尚未被 Worker 取走」的任务有效；已经在执行的任务撤销不了（要强杀才用 `terminate=True`，且会杀掉 Worker 子进程，不建议）。
- Worker **重启后撤销名单丢失**（默认存在内存里）。需要跨重启生效，给 Worker 加 `--statedb`：

```bash
celery -A celery_reminder.celery_app:app worker -l INFO --statedb=/var/run/celery/worker.state
```

> 实战中更推荐「软撤销」：任务照常触发，但任务体第一行查一下 `remind:cancel:{remind_id}` 是否存在，存在就直接 return。它不依赖 Worker 状态，重启也不失效。

## 3.4 这一层留下的问题

- 提醒只存了「任务 id」，业务上想按「谁的提醒」来查询/撤销，得自己维护映射；
- Worker 挂了、任务被反复重试时，同一提醒可能执行多次（第 7 章解决）；
- 没有任何周期能力 → 进入第 4 章。

***

# 第 4 章 第 2 层｜静态周期提醒（beat\_schedule）

## 4.1 把定时表写进代码

```python
app.conf.beat_schedule = {
    # 键名 = 定时条目名（唯一标识，别重复）
    "agent-001-session-check": {
        "task": "agent.reminder.deliver",          # 必须与 @app.task(name=...) 一致
        "schedule": 180,                            # 单位：秒
        "args": ({"agent_id": "agent-001", "text": "会话巡检"},),
    },
    "agent-heartbeat-hourly": {
        "task": "agent.reminder.deliver",
        "schedule": timedelta(minutes=60),          # 也接受 timedelta
        "args": ({"agent_id": "agent-001", "text": "心跳"},),
    },
}
```

`args` 必须是 **tuple**。写成 `"args": [ {...} ]`（list）时 Celery 会照单接受，但在某些版本/序列化组合下会变成「把整个 dict 当成一个位置参数」的惊喜，直接按约定写 tuple 最稳。

## 4.2 crontab：日历式定时

```python
from celery.schedules import crontab

app.conf.beat_schedule = {
    # 每天 02:00
    "daily-cleanup": {
        "task": "agent.reminder.deliver",
        "schedule": crontab(hour=2, minute=0),
        "args": ({"text": "每日清理"},),
    },
    # 每周一 09:00
    "monday-standup": {
        "task": "agent.reminder.deliver",
        "schedule": crontab(hour=9, minute=0, day_of_week=1),
        "args": ({"text": "周会提醒"},),
    },
    # 每小时的第 5 分钟（xx:05）
    "hourly-5": {
        "task": "agent.reminder.deliver",
        "schedule": crontab(minute=5),
        "args": ({"text": "整点后巡检"},),
    },
    # 工作日 18:30（多个值用列表）
    "weekday-evening": {
        "task": "agent.reminder.deliver",
        "schedule": crontab(hour=18, minute=30, day_of_week=[1, 2, 3, 4, 5]),
        "args": ({"text": "下班前收尾"},),
    },
}
```

**`day_of_week`** **编号已实测确认**：`0=周日, 1=周一, 2=周二, 3=周三, 4=周四, 5=周五, 6=周六`（与 Linux cron 一致）。也可以写 `"mon"` / `"monday"` 这种字符串，可读性更好。**最容易写错的是「周一」写成 0**——那是周日，任务会提前一天跑，而且因为「确实跑了」，监控看不出异常。

## 4.3 双进程启动

```bash
# 终端 1：干活的
celery -A celery_reminder.celery_app:app worker -l INFO

# 终端 2：闹钟
celery -A celery_reminder.celery_app:app beat -l INFO
```

正常日志长这样：

```
[beat] Scheduler: Sending due task agent-001-session-check (agent.reminder.deliver)
[worker] Task agent.reminder.deliver[...] received
[worker] Task agent.reminder.deliver[...] succeeded
```

只看到 beat 的 `Sending` 而没有 worker 的 `received`，说明路由/队列不匹配（见第 10 章）。

## 4.4 静态方案的天花板

| 问题                   | 后果                                        |
| -------------------- | ----------------------------------------- |
| 定时表写在代码里             | 新增一个 Agent 的提醒要改代码 + 重启 beat              |
| 无法在运行时取消某个 Agent 的提醒 | 只能整体重启或留着它瞎跑                              |
| 默认 Scheduler 把表存内存   | beat 重启后 `last_run_at` 丢失，可能立刻重跑一次        |
| 无分布式锁                | 起两个 beat 就双倍提醒（见 4.5）                     |
| 条目数无限膨胀              | 1000 个 Agent × 3 条周期 = 3000 个写死的条目，代码很难维护 |

> **结论**：静态方案适合「全局唯一的、业务上不会变的巡检」（如全库每天凌晨清理）。一旦提醒是**按会话/按 Agent 动态产生**的，就必须换 Scheduler。

## 4.5 生产禁令：beat 只能有一个（除非用 RedBeat）

Celery 默认 Scheduler **没有任何去重机制**。两个 beat 同时跑，同一时刻它们都会算出「这个任务到期了」，于是 Worker 收到两份同样的任务。

- 临时解法：**beat 只部署 1 个实例**，Worker 随便横向扩（绝大多数中小项目够用，也是最省心的做法）。
- 正解：换 RedBeat，它用 Redis 锁保证「同一时刻只有一个 beat 在调度」，见第 5 章。

***

# 第 5 章 第 3 层｜动态周期提醒（RedBeat）

这是**为 Agent 场景量身定做的一层**。Agent 的提醒天然是「会话级」的：用户说「每小时提醒我喝水」，这个提醒属于这次会话；会话结束就该删掉。把这种东西写进 `beat_schedule` 是不可能的。

## 5.1 原生 beat 为什么撑不住

三个硬伤：

1. **定时表在进程内存里**，业务进程（你的 Agent）改不到它——它们根本不是一个进程。
2. **改表要重启 beat**，重启期间所有周期提醒停摆，且 `last_run_at` 可能丢。
3. **表不可共享**，多 beat 只能靠「只部署一个」来保证不重复。

RedBeat 把这三件事一起解决了：**定时表搬进 Redis、任何进程都能读写、beat 每次 tick 重新从 Redis 读表、读表时用锁保证只有一个 beat 在动。**

## 5.2 RedBeat 原理：Redis 里的三样东西

配置 `redbeat_key_prefix`（默认 `redbeat:`）后，Redis 里会出现：

| 键                   | 类型       | 作用                                                      |
| ------------------- | -------- | ------------------------------------------------------- |
| `redbeat::schedule` | **ZSET** | 定时表索引：member = 条目 key，score = 下次执行时间戳（秒）                |
| `redbeat:<条目名>`     | **HASH** | 单个条目的定义：`definition`(任务名/参数/周期) + `meta`(last\_run\_at) |
| `redbeat::lock`     | String   | 分布式锁，防止多个 beat 同时调度                                     |
| `redbeat::statics`  | Set      | 记录哪些条目来自 `beat_schedule`（静态条目），用于启动时清理                  |

> 键名里的双冒号是 RedBeat 的实现细节（`key_prefix + ":schedule"` + 默认前缀自带尾冒号），不是笔误。**这也是为什么建议你直接使用默认前缀、靠独立的 Redis DB 做隔离**，而不是自己拼前缀——自己拼很容易拼出 `redbeat:agent` 这样的前缀，结果条目 key 变成 `redbeat:agenta1-daily`（少了分隔符），排查时一脸问号。

调度循环（`tick`）做的事：

```
读锁 → 从 ZSET 里取 score <= now 的条目（即到期条目）
     → 顺带看一眼 max_interval 窗口内即将到期的条目（用于算下次睡多久）
     → 逐条投递到 Broker，投递后把该条目的 score 推到下次执行时间
     → 释放/续期锁 → 睡 min(最近到期时间, max_interval)
```

**关键推论（第 10 章的坑就是这么来的）**：beat 每次最多睡 `max_interval` 秒，Celery 默认 `beat_max_loop_interval=0` 时这个上限是 **300 秒**。也就是说，你从业务进程新加一条周期提醒，**beat 最长要 5 分钟才感知到**。

```python
# 想让动态增删「秒级生效」，把这个调小（代价是 beat 空转更频繁）
app.conf.beat_max_loop_interval = 5
```

已实测：`beat_max_loop_interval=5` 时 `Scheduler.max_interval` 从 300 变成 5。

## 5.3 正确配置

```python
from celery import Celery

app = Celery("celery_reminder")
app.conf.update(
    broker_url="redis://127.0.0.1:6379/0",
    task_ignore_result=True,
    timezone="Asia/Shanghai",
    enable_utc=True,
    include=["celery_reminder.tasks"],

    # ---------- RedBeat ----------
    # 定时表用的 Redis：建议独立 DB 或独立实例，别和业务缓存抢内存
    redbeat_redis_url="redis://127.0.0.1:6379/2",
    # 保持默认前缀，隔离靠上面的 DB，而不是改前缀
    redbeat_key_prefix="redbeat:",
    # 锁超时：不设则 = max_interval * 5；周期很长时可显式给
    redbeat_lock_timeout=60,
    # 动态增删的感知窗口
    beat_max_loop_interval=30,
)
```

> <br />

启动 beat 时**必须显式指定 Scheduler**，否则 RedBeat 完全不生效（这是最高频的「配了没反应」原因）：

```bash
celery -A celery_reminder.celery_app:app beat -l INFO \
       --scheduler redbeat.RedBeatScheduler
```

`--scheduler` 也可以写在配置里，避免每次敲：

```python
app.conf.beat_scheduler = "redbeat.RedBeatScheduler"
```

## 5.4 增 / 删 / 停 / 查

### 新增（或覆盖同一条）

```python
from redbeat import RedBeatSchedulerEntry
from celery.schedules import crontab

# 固定间隔：每 10 分钟
entry = RedBeatSchedulerEntry(
    name="remind-agent001-water",                     # 条目名，唯一；也是 key 的一部分
    task="agent.reminder.deliver",                     # 必须与注册名一致
    schedule=600,                                      # 秒；也接受 timedelta / crontab
    args=({"agent_id": "agent-001", "text": "喝水提醒"},),
    app=app,
)
entry.save()      # ← 写入 Redis，beat 下次 tick 就能看到

# 日历式：每天 09:00（entry 的 schedule 直接给 crontab 对象）
entry2 = RedBeatSchedulerEntry(
    name="remind-agent001-morning",
    task="agent.reminder.deliver",
    schedule=crontab(hour=9, minute=0),
    args=({"agent_id": "agent-001", "text": "早会提醒"},),
    app=app,
)
entry2.save()
```

`name` 相同再 `save()` 就是**覆盖更新**（定义会被替换，但 `last_run_at` 走 `hsetnx` 保留）。所以「修改周期」不用先删后建。

### 删除

```python
from redbeat import RedBeatSchedulerEntry

name = "remind-agent001-water"
key = RedBeatSchedulerEntry.generate_key(app, name)   # 必须带上前缀
try:
    RedBeatSchedulerEntry.from_key(key, app=app).delete()
except KeyError:
    pass          # 本来就不存在
```

> **高频错误**：`from_key("remind-agent001-water")` 会直接 `KeyError`，因为 `from_key` 要的是**完整 Redis key**（含前缀），不是条目名。口诀：`from_key` 配 `generate_key`。
> 另外：`save()` 内部会 `ZADD` 到 `redbeat::schedule`，`delete()` 会 `ZREM` 并对条目 key 做 `DEL`，所以**别用** **`redis-cli DEL`** **手删**——只删条目、不删索引，beat 会加载失败并告警。

### 暂停但保留定义

```python
entry = RedBeatSchedulerEntry.from_key(key, app=app)
entry.enabled = False
entry.save()          # 再置 True 即恢复
```

### 查看当前所有周期提醒

```python
import redis
from redbeat import RedBeatSchedulerEntry

rds = redis.Redis.from_url(app.conf.redbeat_redis_url)
sched_key = app.conf.redbeat_key_prefix + ":schedule"

for raw_key in rds.zrange(sched_key, 0, -1):
    e = RedBeatSchedulerEntry.from_key(raw_key, app=app)
    due, seconds_to_next = e.is_due()
    print({
        "name": e.name,
        "task": e.task,
        "schedule": str(e.schedule),
        "enabled": e.enabled,
        "next_run_in": int(seconds_to_next),
        "next_run_at": int(rds.zscore(sched_key, raw_key)),
    })
```

运维侧等价命令（不用写代码就能看）：

```bash
redis-cli -n 2 zrange redbeat::schedule 0 -1 withscores   # 到期时间戳一览
redis-cli -n 2 hget  redbeat::remind-agent001-water definition
```

## 5.5 与静态 `beat_schedule` 共存

可以，而且在迁移期很有用：**全局固定的用静态，会话级的用动态**。

RedBeat 启动时会读 `app.conf.beat_schedule`，把这些条目写进 Redis 并在 `redbeat::statics` 里登记；配置里删掉的条目，下次启动会被清理。所以：

- 静态条目 = 由 `beat_schedule` 托管，**不要**在运行期用代码去改它（改了也会被下次启动覆盖）；
- 动态条目 = 只用 `RedBeatSchedulerEntry` 管，**不要**写进 `beat_schedule`；
- 两者名字**不能重名**（同 key 会互相覆盖）。

## 5.6 五个高频错误

| 现象                     | 原因                                             | 处理                           |
| ---------------------- | ---------------------------------------------- | ---------------------------- |
| 加了条目但一直不跑              | beat 没带 `--scheduler redbeat.RedBeatScheduler` | 补参数或配 `beat_scheduler`       |
| 加了条目要等好几分钟才生效          | `max_interval` 默认 300s                         | 调 `beat_max_loop_interval=5` |
| 删除报 `KeyError`         | 传了条目名而非完整 key                                  | `generate_key` 拼前缀           |
| 同一条目在两个环境互相打架          | 两个环境共用同一 Redis DB                              | 按环境隔离 DB / 实例                |
| beat 日志刷「lock timeout」 | 条目周期远大于 `redbeat_lock_timeout`                 | 显式调大或留空用默认 `max_interval*5`  |

***

# 第 6 章 第 4 层｜工程化封装（ReminderManager）

到这里你已经会写散装代码了。这一章把它收成一个**业务侧只调三个方法**的模块。

## 6.1 目录结构

```
celery_reminder/
├── celery_app.py       # 实例 + 配置（唯一事实来源）
├── tasks.py            # 任务定义（只做投递，见第 8 章）
├── reminder.py         # ReminderManager：一次性 / 周期 / 撤销 / 查询
└── events.py           # Agent 侧事件消费（可选，第 8 章给）
```

## 6.2 `celery_app.py`

```python
from celery import Celery

app = Celery("celery_reminder")

app.conf.update(
    # 三个 Redis DB 各管一摊，互不干扰：
    # db0 = Broker（任务消息）  db1 = 业务状态（幂等/撤销/锁/事件流）  db2 = 定时表
    broker_url="redis://127.0.0.1:6379/0",
    state_redis_url="redis://127.0.0.1:6379/1",      # 自定义配置项，Celery 允许
    redbeat_redis_url="redis://127.0.0.1:6379/2",
    redbeat_key_prefix="redbeat:",
    beat_max_loop_interval=5,
    beat_scheduler="redbeat.RedBeatScheduler",

    task_ignore_result=True,
    task_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    include=["celery_reminder.tasks"],

    # 队列分离：提醒任务独立队列，避免被其他重任务饿死
    task_default_queue="reminders",
    task_routes={"agent.reminder.*": {"queue": "reminders"}},

    # 重试策略（投递失败时可以重试，任务体必须幂等）
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)
```

## 6.3 `tasks.py`

```python
import json

import redis

from celery_reminder.celery_app import app

_redis = redis.Redis.from_url(app.conf.state_redis_url, decode_responses=True)
EVENT_STREAM = "agent:events"


@app.task(
    name="agent.reminder.deliver",
    ignore_result=True,
    acks_late=True,
    soft_time_limit=10,
    time_limit=30,
    autoretry_for=(ConnectionError,),
    retry_backoff=2,
    retry_jitter=True,
    max_retries=5,
)
def deliver_reminder(payload: dict) -> None:
    """把一条提醒写进事件流；Agent 侧自己消费。任务体要能安全重跑。"""
    remind_id = payload.get("remind_id")
    if remind_id:
        # 幂等闸门：同一条提醒只投递一次（Redis SET NX）
        if not _redis.set(f"remind:sent:{remind_id}", "1", nx=True, ex=86400):
            return
    _redis.xadd(
        EVENT_STREAM,
        {"data": json.dumps({"type": "reminder", "payload": payload}, ensure_ascii=False)},
        maxlen=100_000,
        approximate=True,
    )
```

## 6.4 `reminder.py`（完整可跑）

```python
"""Agent 提醒的统一入口：一次性 / 定点 / 周期，全部走这里。"""
from __future__ import annotations

import uuid
from datetime import datetime

import redis
from celery.schedules import crontab
from redbeat import RedBeatSchedulerEntry

from celery_reminder.celery_app import app
from celery_reminder.tasks import deliver_reminder

ONCE_KEY_PREFIX = "remind:once:"
PERIODIC_NAME_PREFIX = "remind-s-"


class ReminderManager:
    def __init__(self, celery_app=None):
        self.app = celery_app or app
        # 业务状态库：一次性提醒的 id 映射、撤销标记
        self.state = redis.Redis.from_url(
            self.app.conf.state_redis_url, decode_responses=True
        )
        # 定时表库：只在巡检/查询时需要
        self.sched = redis.Redis.from_url(
            self.app.conf.redbeat_redis_url, decode_responses=True
        )

    # ---------------- 一次性 ----------------
    def once_in(self, payload: dict, delay_seconds: int, remind_id: str | None = None) -> str:
        """delay_seconds 秒后提醒一次。"""
        remind_id = remind_id or uuid.uuid4().hex
        body = {**payload, "remind_id": remind_id}
        res = deliver_reminder.apply_async(
            args=[body], countdown=int(delay_seconds), queue="reminders"
        )
        # 存 task_id，撤销时用；TTL 略大于提醒窗口，自动清理
        self.state.setex(f"{ONCE_KEY_PREFIX}{remind_id}", 7 * 86400, res.id)
        return remind_id

    def once_at(self, payload: dict, at: datetime, remind_id: str | None = None) -> str:
        """在某个绝对时刻提醒一次。at 必须带时区。"""
        if at.tzinfo is None:
            raise ValueError("at 必须带时区，例如 ZoneInfo('Asia/Shanghai')")
        remind_id = remind_id or uuid.uuid4().hex
        body = {**payload, "remind_id": remind_id}
        res = deliver_reminder.apply_async(args=[body], eta=at, queue="reminders")
        self.state.setex(f"{ONCE_KEY_PREFIX}{remind_id}", 7 * 86400, res.id)
        return remind_id

    def cancel_once(self, remind_id: str) -> bool:
        """撤销尚未触发的一次性提醒。软撤销 + 广播撤销双保险。"""
        key = f"{ONCE_KEY_PREFIX}{remind_id}"
        task_id = self.state.get(key)
        # 软撤销：任务万一已经触发，任务体/消费侧会看到这个标记而跳过
        self.state.set(f"remind:cancel:{remind_id}", "1", ex=86400)
        if not task_id:
            return False
        self.app.control.revoke(task_id, terminate=False)
        self.state.delete(key)
        return True

    # ---------------- 周期 ----------------
    def every(
        self,
        payload: dict,
        seconds: int | None = None,
        cron: crontab | None = None,
        remind_id: str | None = None,
    ) -> str:
        """周期提醒：seconds 给固定间隔，cron 给日历式，二者只能给一个。"""
        if (seconds is None) == (cron is None):
            raise ValueError("seconds 与 cron 必须且只能给一个")
        remind_id = remind_id or uuid.uuid4().hex
        name = f"{PERIODIC_NAME_PREFIX}{remind_id}"
        entry = RedBeatSchedulerEntry(
            name=name,
            task=deliver_reminder.name,
            schedule=cron if cron is not None else int(seconds),
            args=({**payload, "remind_id": remind_id},),
            app=self.app,
        )
        entry.save()
        return remind_id

    def cancel_every(self, remind_id: str) -> bool:
        name = f"{PERIODIC_NAME_PREFIX}{remind_id}"
        key = RedBeatSchedulerEntry.generate_key(self.app, name)
        try:
            RedBeatSchedulerEntry.from_key(key, app=self.app).delete()
            return True
        except KeyError:
            return False

    def pause_every(self, remind_id: str, paused: bool = True) -> bool:
        name = f"{PERIODIC_NAME_PREFIX}{remind_id}"
        key = RedBeatSchedulerEntry.generate_key(self.app, name)
        try:
            entry = RedBeatSchedulerEntry.from_key(key, app=self.app)
        except KeyError:
            return False
        entry.enabled = not paused
        entry.save()
        return True

    def list_every(self) -> list[dict]:
        sched_key = self.app.conf.redbeat_key_prefix + ":schedule"
        out = []
        for raw_key in self.sched.zrange(sched_key, 0, -1):
            try:
                entry = RedBeatSchedulerEntry.from_key(raw_key, app=self.app)
            except KeyError:
                continue
            _, seconds_to_next = entry.is_due()
            out.append(
                {
                    "name": entry.name,
                    "remind_id": entry.name[len(PERIODIC_NAME_PREFIX):],
                    "task": entry.task,
                    "schedule": str(entry.schedule),
                    "enabled": entry.enabled,
                    "next_run_in": int(seconds_to_next),
                    "next_run_at": int(self.sched.zscore(sched_key, raw_key) or 0),
                }
            )
        return out


reminder = ReminderManager()
```

> 说明：`args` 里统一注入 `remind_id`，是为了让任务体做幂等与「软撤销」（见 7.1）。任务只接收一个 dict 参数，参数结构固定，后续加字段不用改任务签名。

## 6.5 业务侧怎么用

```python
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from celery.schedules import crontab

from celery_reminder.reminder import reminder

# 用户说：「20 分钟后提醒我再看一眼这个任务」
rid1 = reminder.once_in(
    {"agent_id": "agent-001", "session_id": "s-42", "text": "任务 T-9001 该复查了"},
    delay_seconds=20 * 60,
)

# 用户说：「今天 15:30 提醒我」
rid2 = reminder.once_at(
    {"agent_id": "agent-001", "session_id": "s-42", "text": "15:30 评审会"},
    at=datetime(2026, 11, 11, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
)

# 用户说：「以后每天早上 9 点提醒我看日报」
rid3 = reminder.every(
    {"agent_id": "agent-001", "session_id": "s-42", "text": "看日报"},
    cron=crontab(hour=9, minute=0),
)

# 会话结束时，把这个会话的周期提醒清干净
reminder.cancel_every(rid3)

# 运维看一眼当前所有周期提醒
print(reminder.list_every())
```

这五行调用覆盖了 95% 的「Agent 提醒」产品需求，而且**不需要重启任何进程**。

***

# 第 7 章 第 5 层｜可靠性与正确性

上一章解决了「能不能用」，这一章解决「会不会出错」。提醒这件事的容错要求比普通任务高，因为**错误是用户能直接感知的**：漏一条是「怎么没提醒我」，重复十条是「怎么刷屏」。

## 7.1 幂等：同一条提醒只该生效一次

消息队列的语义是 **at-least-once**，不是 exactly-once。所以任务体重跑是常态，不是异常：

- Worker 在执行中断（OOM、被 kill、`acks_late` 已签收但进程死了）；
- 网络抖动导致 `autoretry_for` 重试；
- 人为重放。

两道闸门（代码已在 6.3 / 6.4 埋好）：

```python
def guard_delivery(remind_id: str) -> bool:
    """闸门 1：投递侧（Worker 里）。返回 True 表示首次投递，可以继续。"""
    return bool(_redis.set(f"remind:sent:{remind_id}", "1", nx=True, ex=86400))


def guard_agent_action(remind_id: str) -> bool:
    """闸门 2：消费侧（Agent 里），紧贴「给用户发消息」之前。"""
    return bool(_redis.set(f"agent:done:{remind_id}", "1", nx=True, ex=86400))
```

两道的区别：

- 闸门 1 防「通知重复投递」；
- 闸门 2 防「Agent 处理重复事件导致重复发消息给用户」。

**闸门 2 才是用户可感知的那一层**，因为它紧贴着「发消息」这个副作用。副作用在哪，闸门就放在哪的上一层。

## 7.2 单 Agent 串行：别让两条提醒同时唤醒同一个 Agent

Agent 往往是「一次处理一个会话」的状态机。如果 3 条提醒恰好同时到期，Worker 并发度 > 1，三条任务会同时调 Agent，直接打架。

```python
import time

LOCK_PREFIX = "agent:lock:"

@app.task(name="agent.reminder.deliver")
def deliver_reminder(payload: dict) -> None:
    agent_id = payload["agent_id"]
    lock_key = f"{LOCK_PREFIX}{agent_id}"
    token = uuid.uuid4().hex
    # 简单版：ex 覆盖单次处理最大时长；拿不到就放弃本轮（下一周期还会来）
    if not _redis.set(lock_key, token, nx=True, ex=15):
        return
    try:
        ...  # 处理
    finally:
        # 释放要校验 token，防止误删别人的锁
        if _redis.get(lock_key) == token:
            _redis.delete(lock_key)
```

更严谨的释放用 Lua（比较并删除原子化），避免「判断通过后锁过期、删掉别人新锁」的时间窗：

```python
RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_redis.eval(RELEASE_LUA, 1, lock_key, token)
```

> **注意区分两把锁**（这是最容易被混为一谈的）：
>
> - **RedBeat 的锁**（`redbeat::lock`）：防「多个 beat 重复生成任务」，是调度层的锁，你不需要管；
> - **业务锁**（`agent:lock:*`）：防「同一个 Agent 被并发唤醒」，是执行层的锁，必须自己加。

## 7.3 「丢」与「重」的边界必须写清楚

| 场景                | 会丢吗             | 会重吗                 | 兜底                   |
| ----------------- | --------------- | ------------------- | -------------------- |
| Redis 重启、无持久化     | **丢**（排队中的提醒全没） | —                   | 开 AOF everysec       |
| Redis 主从切换（消息未同步） | 可能丢             | —                   | 用哨兵/集群 + `wait` 副本确认 |
| Worker 执行中被 kill  | 不丢              | **会重**（`acks_late`） | 幂等闸门                 |
| Beat 重复部署         | 不丢              | **会重**              | RedBeat 锁 / 只部署一个    |
| Worker 长期不可用      | 不丢（堆积）          | —                   | 监控队列长度告警             |

**结论**：宁可「重」，不要「丢」；然后用幂等把「重」的代价压到零。反过来（`acks_late=False` 即先签收后执行）会换来「丢」，而提醒丢了是查不出来的。

## 7.4 超时与重试

```python
import redis


@app.task(
    bind=True,
    name="agent.reminder.deliver",
    ignore_result=True,
    acks_late=True,
    soft_time_limit=10,       # 抛 SoftTimeLimitExceeded，任务可 try/except 收尾
    time_limit=30,            # 硬杀
    autoretry_for=(ConnectionError, redis.exceptions.TimeoutError),
    retry_backoff=2,          # 2s, 4s, 8s...
    retry_jitter=True,        # 打散，避免重试风暴
    max_retries=5,
)
def deliver_reminder(self, payload: dict) -> None:
    ...   # 实现见 6.3
```

原则：

- **只对「可恢复错误」重试**（网络、Redis 抖动）。对 `ValueError` 这种逻辑错误重试 5 次只是把错误延后；
- `soft_time_limit` 一定要小于 `time_limit`，留出收尾时间；
- 提醒任务的执行体应当**极短**（写一条事件流），所以 10 秒足够。如果你的提醒任务要跑 1 分钟，说明架构错了（见第 8 章）。

## 7.5 Redis 侧配置清单

```conf
# redis.conf 关键项
appendonly yes
appendfsync everysec
maxmemory-policy noeviction        # 绝对不能是 allkeys-lru
save 900 1                         # RDB 兜底
# 连接数：worker 并发数 × 每进程连接数 + beat + 业务
maxclients 10000
```

监控指标（这几个数值异常就是提醒要出事的先兆）：

| 指标   | 命令                            | 关注点                  |
| ---- | ----------------------------- | -------------------- |
| 队列积压 | `LLEN reminders`              | 持续上涨 = Worker 不够/挂了  |
| 内存   | `INFO memory`                 | 接近 maxmemory 时后果是写失败 |
| 淘汰   | `INFO stats` 的 `evicted_keys` | **必须为 0**，非 0 就是丢消息  |
| 慢查询  | `SLOWLOG GET 10`              | 提醒链路不该出现慢命令          |

## 7.6 可观测性

```bash
pip install flower
celery -A celery_reminder.celery_app:app flower --port=5555
```

Flower 能看到：任务成功率、执行耗时、队列长度、Worker 在线情况。**用 RedBeat 时注意**：Flower 看不到「定时表」（它只认 default Scheduler 的 `beat_schedule`），要看周期提醒请用 5.4 的 `list_every()`，或直接查 ZSET。

自己埋一条最小监控（每天跑一次，异常告警）：

```python
@app.task(name="agent.reminder.healthcheck")
def healthcheck() -> dict:
    sched = redis.Redis.from_url(app.conf.redbeat_redis_url, decode_responses=True)
    broker = redis.Redis.from_url(app.conf.broker_url, decode_responses=True)
    return {
        "periodic_entries": sched.zcard(app.conf.redbeat_key_prefix + ":schedule"),
        "queue_backlog": broker.llen("reminders"),
        "evicted_keys": int(broker.info("stats").get("evicted_keys", 0)),
    }
```

`evicted_keys > 0` 是红线：说明 Redis 已经在偷偷删你的消息了。

***

# 第 8 章 第 6 层｜架构：提醒任务只做投递

## 8.1 为什么不能在 Celery 任务里直接跑 Agent

一个诱人但错误的写法：

```python
@app.task
def remind(payload):
    agent = load_agent(payload["agent_id"])   # 反序列化整个 Agent
    agent.run(payload["text"])                # 跑几十秒甚至几分钟
```

四个致命问题：

1. **状态不同步**：Worker 进程里的「Agent」是另一个进程内存里的副本，跟用户正在对话的那个 Agent 不是同一个对象。你在副本里改的状态，用户侧看不到。
2. **资源浪费**：为了发一句提醒，要加载完整 Agent（模型客户端、工具、记忆）。Worker 并发一开，机器直接打满。
3. **阻塞调度**：任务跑 3 分钟，队列积压；提醒开始延迟，而提醒最不能延迟。
4. **不可控**：Worker 扩缩容会「顺手」改变 Agent 的并发度，两件事耦合在一起。

## 8.2 正确形态：Worker 只写事件，Agent 自己消费

```
Celery Worker  →  XADD agent:events  →  Agent 进程 XREADGROUP 消费
   （毫秒级）        （Redis Stream）        （在 Agent 自己的上下文里处理）
```

好处：

- Celery 的职责退化成「按时投递一条消息」，**失败面极小**（Redis 写一个 entry）；
- 提醒的处理发生在**用户真正在用的那个 Agent 进程里**，状态天然一致；
- Worker 与 Agent 可独立扩缩容；
- 消费侧有 ack 语义，处理失败可重投，可做「已读/未读」与顺序保证。

`events.py`（Agent 侧消费骨架）：

```python
import json
import redis

from celery_reminder.celery_app import app

rds = redis.Redis.from_url(app.conf.state_redis_url, decode_responses=True)
STREAM, GROUP = "agent:events", "agents"

try:
    rds.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
except redis.ResponseError:
    pass  # BUSYGROUP：已存在


def consume_once(consumer: str, block_ms: int = 5000):
    resp = rds.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=10, block=block_ms)
    for _, messages in resp or []:
        for msg_id, fields in messages:
            event = json.loads(fields["data"])
            try:
                handle_event(event)          # ← 真正的提醒动作（发消息、唤醒 Agent）
            except Exception:
                # 不 ack 的话会留在 pending，可人工/定时 reclaim
                continue
            rds.xack(STREAM, GROUP, msg_id)
```

配套两个运维动作：

```bash
# 查看积压与 pending
redis-cli XINFO GROUPS agent:events
redis-cli XPENDING agent:events agents
```

> 若提醒量级很小（每天几百条），用 **Redis List + BLPOP** 也够，甚至直接在 Agent 里起个本地的轻量循环；Stream 的价值在于「消费组 + ack + pending 可查」，提醒量大、多个 Agent 实例时更稳。

## 8.3 分层职责表（记住这张表就不会写歪）

| 层        | 职责                                | 不该做的事                |
| -------- | --------------------------------- | -------------------- |
| Beat     | 算时间、投递任务                          | 不碰业务、不加载 Agent       |
| Worker   | 执行 `deliver_reminder`：幂等判断 + 写事件流 | 不跑 Agent、不调模型、不发用户消息 |
| Agent 进程 | 消费事件、决定「怎么提醒」、发消息                 | 不自己算时间、不自己轮询         |
| Redis    | 消息 + 定时表 + 幂等键                    | 不当缓存池（别混业务缓存）        |

***

# 第 9 章 选型决策表 & 什么时候不该用 Celery

## 9.1 决策表

| 你的需求               | 方案                 | beat     | 代码入口                |
| ------------------ | ------------------ | -------- | ------------------- |
| 用户说「N 分钟后提醒我」      | `countdown`        | 不需要      | `reminder.once_in`  |
| 用户说「今天 15:30 提醒我」  | `eta`              | 不需要      | `reminder.once_at`  |
| 全局固定巡检（每天/每小时）     | 静态 `beat_schedule` | 需要（单实例）  | 配置                  |
| 每个会话/每个 Agent 独立周期 | **RedBeat**        | 需要（可多实例） | `reminder.every`    |
| 需要后台页面配置定时         | RedBeat + 自己写管理接口  | 需要       | `reminder.*` + 列表接口 |
| 定时规则要审计/多人协作编辑     | 数据库存规则（不必是 Django） | 需要       | 自研 Scheduler 或换系统   |
| 每天几十条提醒、单机         | 见 9.2，甚至不用 Celery  | 不需要      | 自研 ZSET             |

## 9.2 附：不用 Celery 的轻量方案（Redis ZSET 调度器）

如果提醒量很小、你又不想多维护两个进程（worker + beat），可以用 60 行自己搭。核心就是「ZSET 存到期时间 + 原子取出」：

```python
import json
import time
import uuid

import redis

rds = redis.Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
ZSET = "remind:zset"

POP_DUE = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 50)
if #due == 0 then return {} end
for i = 1, #due do
    redis.call('ZREM', KEYS[1], due[i])
end
return due
"""


def once_in(payload: dict, delay_seconds: int) -> str:
    rid = uuid.uuid4().hex
    member = json.dumps({"rid": rid, "payload": payload}, ensure_ascii=False)
    rds.zadd(ZSET, {member: time.time() + delay_seconds})
    return rid


def run_forever(handle):
    while True:
        due = rds.eval(POP_DUE, 1, ZSET, time.time())
        if not due:
            time.sleep(1)
            continue
        for member in due:
            item = json.loads(member)
            handle(item["payload"])       # 这里做幂等
```

优点：少两个进程、少一层序列化、延迟可控（1 秒轮询）。
缺点：**没有** 重试、超时、worker 池、监控、路由、任务状态——这些 Celery 都是白送的。所以：

> 判断标准：**你需不需要「重试 + 可观测 + 横向扩 Worker」**。需要 → Celery；只是「到点叫醒一下」→ ZSET 足够。

## 9.3 Celery vs 自研 vs 云调度

| 维度    | Celery + Redis      | 自研 ZSET  | 云厂商定时（如函数定时触发） |
| ----- | ------------------- | -------- | -------------- |
| 学习成本  | 中（beat/RedBeat 概念多） | 低        | 低              |
| 动态增删  | RedBeat 支持          | 天然支持     | 一般要调 API       |
| 精度    | 秒级                  | 秒级（轮询粒度） | 分钟级常见          |
| 重试/监控 | 内置                  | 全自己写     | 平台提供           |
| 大规模   | 好（可扩 worker）        | 轮询压力大    | 好              |
| 多环境隔离 | 靠 Redis DB/实例       | 同        | 天然             |

***

# 第 10 章 排错手册

按「现象」查，比按「组件」查快。

| #  | 现象                                               | 最可能原因                                          | 处理                                              |
| -- | ------------------------------------------------ | ---------------------------------------------- | ----------------------------------------------- |
| 1  | beat 日志有 `Sending due task`，worker 毫无反应          | 队列名不匹配 / `task_routes` 指到别的队列                  | worker 加 `-Q reminders`；或去掉路由                   |
| 2  | worker 报 `NotRegistered: agent.reminder.deliver` | 任务模块没被 import                                  | 配 `include=[...]` 或 `autodiscover_tasks`        |
| 3  | 定时整体差 8 小时                                       | 没配 `timezone`（默认 UTC）                          | `timezone="Asia/Shanghai"` + `enable_utc=True`  |
| 4  | 定点提醒永远差 8 小时、周期提醒正常                              | `eta` 传了 naive datetime                        | 用带 `tzinfo` 的 datetime                          |
| 5  | 同一提醒被执行了两次/多次                                    | 起了两个 beat；或 Worker 重试                          | 静态方案只留一个 beat；或用 RedBeat；补幂等                    |
| 6  | 周一的任务在周日跑                                        | `day_of_week` 写成 0（0=周日）                       | 周一用 1                                           |
| 7  | RedBeat 加了条目但不生效                                 | beat 没带 `--scheduler redbeat.RedBeatScheduler` | 补参数或配 `beat_scheduler`                          |
| 8  | 动态新增要等好几分钟才跑                                     | `max_interval` 默认 300s                         | `beat_max_loop_interval=5`                      |
| 9  | 删除条目 `KeyError`                                  | `from_key` 传了名字，缺前缀                            | `RedBeatSchedulerEntry.generate_key(app, name)` |
| 10 | beat 报 `Failed to load ..., removing`            | 有人 `redis-cli DEL` 手删了条目，索引还在                  | 别手删；用 `entry.delete()`                          |
| 11 | 队列长度只涨不降                                         | Worker 没起 / 并发太低 / 任务卡死                        | 看 `LLEN`、Flower、`time_limit`                    |
| 12 | 部分提醒「凭空消失」                                       | `maxmemory-policy=allkeys-lru` 静默淘汰            | 改 `noeviction`；看 `evicted_keys`                 |
| 13 | Redis 重启后排队提醒全丢                                  | 未开持久化                                          | `appendonly yes`                                |
| 14 | 同一 Agent 状态错乱                                    | 多条提醒并发处理                                       | 加 `agent:lock:*`（7.2）                           |
| 15 | `redbeat::lock` 频繁超时                             | 周期远大于 `redbeat_lock_timeout`                   | 显式调大                                            |
| 16 | Worker 关了任务就「没了」                                 | `task_acks_late=False`（先签收）                    | 改 `True` + 幂等                                   |
| 17 | 任务体里报 `pickle` 反序列化错误                            | `task_serializer` 没统一                          | 全部锁 `json`                                      |
| 18 | 时间到但提示「任务已过期」（Redis broker）                      | `visibility_timeout` 小于延迟窗口                    | 见下方说明                                           |

**关于第 18 条**：Redis Broker 的 `visibility_timeout`（默认 3600 秒）原本用于「Worker 掉线后把未确认消息还给队列」。它跟 `eta`/`countdown` 组合时，超长延迟（如 3 天后）在某些版本会触发消息被重新投递的边界情况。如果要做**小时级以上**的延迟，两种做法更稳：

- 把延迟调度交给 RedBeat 的周期表（每 N 小时触发一次，业务侧判「是否该提醒」）；
- 或对超长延迟自己存 ZSET（9.2），到点再投递。

***

# 第 11 章 自测题

1. `apply_async(countdown=60)` 需要启动 beat 吗？为什么？
2. beat 挂了，已经投递到 Redis 的提醒还会执行吗？周期提醒还会产生新的吗？
3. 为什么「每 86400 秒」不等于「每天 9 点」？
4. RedBeat 靠什么保证多个 beat 实例不会重复投递？它和业务侧的 Agent 锁有什么区别？
5. `from_key` 为什么必须配 `generate_key`？
6. 动态新增一条周期提醒，为什么可能等 5 分钟才生效？怎么改？
7. 为什么提醒任务里不应该直接调用 Agent？
8. 消息队列是 at-least-once，那「不重复提醒用户」靠什么保证？闸门应该放在哪一层？
9. `maxmemory-policy=allkeys-lru` 对提醒链路的实际危害是什么？为什么日志里看不出来？
10. 如果让你支持「10 万个 Agent 各有一条独立周期提醒」，你会先改哪里？（提示：条目数、beat 扫描、队列分片）

***

# 附录 A 命令速查

```bash
# 启动
celery -A celery_reminder.celery_app:app worker -l INFO -Q reminders
celery -A celery_reminder.celery_app:app beat -l INFO --scheduler redbeat.RedBeatScheduler
celery -A celery_reminder.celery_app:app flower --port=5555

# 运维排查
celery -A celery_reminder.celery_app:app inspect active
celery -A celery_reminder.celery_app:app inspect registered
celery -A celery_reminder.celery_app:app inspect reserved
celery -A celery_reminder.celery_app:app control revoke <task_id>
celery -A celery_reminder.celery_app:app purge -Q reminders     # 危险：清空队列

# Redis 侧
redis-cli -n 2 zrange  redbeat::schedule 0 -1 withscores
redis-cli -n 2 zcard   redbeat::schedule
redis-cli -n 2 hgetall redbeat::remind-s-<rid>
redis-cli -n 0 llen    reminders
redis-cli -n 0 xinfo   groups agent:events
redis-cli -n 0 info    stats | grep evicted
```

# 附录 B 配置项速查

| 配置                                   | 建议值                        | 作用                |
| ------------------------------------ | -------------------------- | ----------------- |
| `broker_url`                         | `redis://host:6379/0`      | 任务消息              |
| `state_redis_url`                    | `redis://host:6379/1`      | 业务状态（幂等/撤销/锁/事件流） |
| `redbeat_redis_url`                  | `redis://host:6379/2`      | 定时表（独立 DB）        |
| `redbeat_key_prefix`                 | `redbeat:`（默认）             | 键前缀，别乱改           |
| `redbeat_lock_timeout`               | 留空或 60                     | beat 锁超时          |
| `beat_scheduler`                     | `redbeat.RedBeatScheduler` | 启用 RedBeat        |
| `beat_max_loop_interval`             | `5`                        | 动态增删的感知窗口         |
| `timezone`                           | `Asia/Shanghai`            | crontab 的解释时区     |
| `enable_utc`                         | `True`                     | 内部统一 UTC          |
| `task_ignore_result`                 | `True`                     | 不写结果键             |
| `task_serializer`                    | `json`                     | 安全、可读             |
| `task_acks_late`                     | `True`                     | 不丢（代价是可能重）        |
| `worker_prefetch_multiplier`         | `1`                        | 长任务下更公平           |
| `task_default_queue`                 | `reminders`                | 与重任务隔离            |
| `broker_connection_retry_on_startup` | `True`                     | Redis 未就绪也能起      |

# 附录 C 术语表

| 术语                  | 含义                                              |
| ------------------- | ----------------------------------------------- |
| Broker              | 消息中间件，这里指 Redis，存待执行任务                          |
| Beat                | 调度器进程，到点投递任务，不执行业务                              |
| Worker              | 执行进程，消费任务                                       |
| RedBeat             | 基于 Redis 的 Scheduler，定时表存 Redis、带分布式锁、支持动态增删    |
| Entry               | RedBeat 里的一条定时记录（name / task / schedule / args） |
| `countdown` / `eta` | 延迟投递的两种表达：相对秒数 / 绝对时刻                           |
| `crontab`           | 日历式表达式，`0=周日`                                   |
| at-least-once       | 至少一次语义：可能重复，不会丢                                 |
| 幂等                  | 同一操作重复执行，结果与执行一次相同                              |
| `max_interval`      | beat 两次 tick 之间最长睡眠，默认 300 秒                    |

***

## 结语：一句话总结

> **`countdown`/`eta`** **解决「一次」，`beat`** **解决「周期」，`RedBeat`** **解决「动态 + 多实例不重复」，Worker 只做「投递」，幂等与业务锁解决「不重不乱」。**
> 四件事分清了，Celery + Redis 的定时调度对 Agent 来说就只剩实现细节。

> （注：部分内容由豆包 AI 生成）
