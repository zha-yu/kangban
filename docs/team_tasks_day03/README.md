# Day 3 五人并行分工总览

> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-05
> 关联：docs/plans/day-03-WebRTC与语音闭环.md（v1.5，唯一执行基线）
> 上游依据：PRD §2.2/§4.11（验收）｜技术设计文档 Day 3 落地清单 line 1019（实现）｜adhoc-20261105-音频传输与设备链路技术方案 v1.3（裁决口径）
> 变更记录见文末

## 1. 人员分工表（工作量均衡：4 个固定单元 + 1 个机动单元）

| 代号 | 单元 | 核心职责 | 固定写作域（唯一所有权） |
| --- | --- | --- | --- |
| **A1** | ASR/TTS 适配层 | Provider 协议、FunASR 适配器、Mock/云骨架、工厂分派 | `app/audio/asr.py`、`app/audio/tts.py`、`app/audio/__init__.py`、`docker-compose.yml`（funasr 服务）、README 第 6 节登记 |
| **A2** | 信令 WS 与鉴权 | WS `/ws/signal` 首帧鉴权、心跳、断线检测、WS trace 注入 | `app/audio/signaling.py`、`app/main.py`（仅路由装配行） |
| **A3** | WebRTC 媒体层 | aiortc 建连、音频轨收发、loop 绑定防护、回声验证 | `app/audio/webrtc.py` |
| **A4** | pad 前端 | MPA 骨架、登录页、AudioWorklet 重采样、TEN-VAD、WebRTC 客户端、大字幕/打断 | `frontend/` 全部（后端文件一律不动） |
| **A5**（机动） | VAD + 降级 + 测试收口 | 固定：webrtcvad 兜底、REST 降级链路、攻击面测试；弹性：阻塞支援、公共组件 | `app/audio/vad.py`、`app/api/audio.py`（降级路由）、`tests/`（Day 3 新增用例） |

**公共文件所有权规则**：`app/core/config.py`（Settings）字段已预留，Day 3 原则上只读；确需新增字段由 A1 统一动并在群内通告。任何人不得改他人写作域文件——集成冲突以此为唯一防线。

## 2. 依赖关系与开发顺序

```
09:00–09:30 契约冻结会（全员）：
  A1 定稿 ASRResult / ASREvent     （app 内部契约）
  A2 定稿 WS 消息表                 （signaling 协议）
  A5 定稿 POST /api/audio/utterance （降级 REST 形状）
  → 三份契约上午定稿后即冻结，变更必须群通告 + 卡上留痕

契约冻结后即可完全并行：
  A1 ──(ASRResult/事件流)──▶ A2、A3、A4 消费
  A2 ──(WS 消息表)──▶ A3（offer/answer/ice 处理）、A4（客户端信令）
  A4 ──(offer)──▶ A3 ──(answer/ice 下行)──▶ A4   ← 14:30 起三方联调
  A5 与 A1/A2 无依赖，全程并行
```

开发顺序：**契约会（09:00）→ A1/A2/A4/A5 立即开工 → A3 于 09:30 放行 → 14:30 三方联调（A4↔A2↔A3，A1 支撑真链路）→ 17:00 收口**。

## 3. 模块间接口契约（要求 4：开发前定稿，联调各自对接）

| 契约 | 冻结人 | 内容要点 | 消费方 |
| --- | --- | --- | --- |
| C1 `ASRResult`/`ASREvent` | A1 | `ASRResult(text: str, is_final: bool, confidence: float\|None)`；事件流 `ASREvent(kind: Literal["partial","final","error"], result)`；错误二分类 `ASRUnavailable`(可退避重试)/`ASRProtocolError`(直接降级) | A2（WS 转发 partial/final）、A3（音频帧喂入）、A5（utterance 用 final） |
| C2 WS 消息表 | A2 | `auth{token}`→`auth_ok`/`auth_fail`(4401)；`offer/answer{sdp}`、`ice{candidate}`；`ping/pong`（20s/45s）；`asr_start/asr_end`；`tts_text{text}` | A3、A4 |
| C3 降级 REST | A5 | `POST /api/audio/utterance`（multipart：16k PCM 整段 + device_token 元数据）→ `{"text": str, "reply": {...}}`（reply 形状随 TTS 决议） | A4（降级模式） |

对接标准：契约用 Python `dataclass`/`TypedDict` 写在对应模块顶部并同提交；跨模块调用只准引用契约类型，不得 import 对方实现细节。

## 4. 时间节点总表

| 时点 | 事件 | 参与人 |
| --- | --- | --- |
| 09:00–09:30 | 契约冻结会：C1/C2/C3 定稿 | 全员 |
| 09:30–12:00 | 各单元并行开发（A3 自 09:30 起） | 全员 |
| 12:30–12:40 | 午站会：进度/阻塞通报 | 全员 |
| 14:30–16:30 | 三方联调：A4↔A2↔A3，A1 支撑 FunASR 真链路 | A1–A4，A5 机动 |
| 17:00–17:30 | 收口会：全量 pytest + README 验收记录 + 一次性提交 | 全员 |

## 5. 每日同步机制（要求 6）

1. **契约冻结会**（晨，30min）：三契约当会定稿，缺席者视为接受——这是全组唯一强同步点。
2. **午站会**（10min）：每人一句话——已完成/进行中/阻塞；阻塞超 30 分钟立即@A5 接手，不等站会。
3. **收口会**（晚，30min）：跑 `uv run pytest -q` 全绿 + `ruff check`，A5 回填 README 第 7 节验收记录，**当天收尾一次性提交**（Conventional Commits，禁止多人交叉半成品提交）。
4. **契约变更纪律**：冻结后任何契约改动必须群通告 + 在自己任务卡「变更记录」留痕；对方未确认前不得合入。
5. **提交纪律**：push 前 `git pull --rebase`；只提交自己写作域文件。

## 6. 验收总口径

单元验收见各任务卡；全组收口以 [day-03 计划](../plans/day-03-WebRTC与语音闭环.md) V1–V6 为准：V1 降级链路闭环、V2 WS 鉴权、V3 回声、V4 全双工/打断、V5 三场景降级话术、V6 覆盖率（核心 Tool/权限 100%、接口 ≥70%）+ README 回填。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-05 | 初稿：五单元拆分、三契约冻结清单、时间节点与同步机制 |
