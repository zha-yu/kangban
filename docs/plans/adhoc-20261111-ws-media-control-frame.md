> 状态：已废弃（2026-11-11，口径并入《接口契约_照护台业务数据.md》§6.5，Q-D11 冻结：维持 D 侧 `media_play`/`media_control{action,media?}`，否决本文 `stream_id`/`media_info` 方案）　｜　版本：v1.0　｜　日期：2026-11-11
> 关联：Day 8 Q-D4（D 提：pad 戏曲 WS 帧形状对齐）+ B4（B 接：play_opera Tool 注册）
> 上游依据：AGENTS §6 模型路由表（L0 规则 → L2 ToolExecutor → WS 下发）｜ Day 3 C2 信令契约（WS /ws/signal 文本帧 JSON 体系不变，本文件定义新增 type）
> 变更记录见文末

---

## 0. 传输层不变

- 通道：**复用 Day 3 已冻结的 `/ws/signal`**（不新建独立 WS 入口）
- 方向：S→C（服务端 → pad），文本帧 JSON，每消息一个对象
- 已有 type（本文件不覆盖，Day 3 C2 契约保留）：`auth` / `auth_ok` / `auth_fail` / `ping` / `pong` / `asr_start` / `asr_partial` / `asr_final` / `tts_start` / `tts_audio` / `tts_end`

本文件新增 **2 种 type**：`media_control` + `media_info`。`tts_interrupt` 也本文件新定，但**走 WebRTC 下行通道**（不经过 WS 媒体控制帧）。

---

## 1. media_control（S→C · 七动作枚举）

**触发方**：`play_opera` Tool 内部经 ToolExecutor 调通后，向 WS /ws/signal 发本帧。

```json
{
  "type": "media_control",
  "trace_id": "uuid-string",
  "stream_id": "uuid-string",
  "rule_id": "R-01",
  "intent": "opera",
  "action": "pause",
  "payload": {}
}
```

### 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | Literal `"media_control"` | ✅ | 帧类型标识，pad 据此路由到播放器 |
| `trace_id` | str (uuid) | ✅ | 全链路 trace，前端日志+后端审计共用 |
| `stream_id` | str (uuid) | ✅ | **WebRTC MediaStream 对应的 track id**——pad 侧用来把动作路由到正确的音频轨（多轨/多会话时不串流） |
| `rule_id` | str | ❌ | 命中规则（如 R-01），前端日志用，业务不依赖 |
| `intent` | str | ❌ | 识别意图，前端日志用 |
| `action` | Literal（见下表） | ✅ | 控制动作枚举 |
| `payload` | dict | ✅ | 预留，当前七动作均 `{}`；未来扩展如 `volume_delta`、`seek_ms` 时可往里加，不破坏旧字段 |

### action 七枚举（与 rules.py r2 + B Tool schema 对齐）

| action | 对应 rule_id | 说明 | pad 播放器行为 |
|---|---|---|---|
| `pause` | R-01 | 暂停播放 | 暂停音频轨，保留 currentTime |
| `resume` | R-02 | 继续播放 | 从 paused 状态恢复 |
| `stop` | R-03 | 停止播放 | 停止 + 清理 currentTime → 0 |
| `next` | R-05 | 换一段 | 触发 `media_info` 加载新资源后从头播 |
| `volume_up` | R-06 | 调大音量 | pad 侧硬编码 `+10dB`（B Tool schema 口径，不暴露 payload） |
| `volume_down` | R-07 | 调小音量 | pad 侧硬编码 `-10dB` |
| `repeat` | R-08 | 重复一遍 | 从头播当前 track |

> **interrupt（R-04）特殊**：走 WebRTC 下行 TTS 中断通道，type=`tts_interrupt`，**不经过 WS media_control**。pad 侧 TTS 播放器直接截停，与媒体控制是两条通道（见 §3）。

---

## 2. media_info（S→C · 换台/换段前下发资源元数据）

**触发方**：Tool 内部决定要切歌（`next`）或首次 `play_opera` 加载时，先下发本帧，pad 拿到后加载资源 → 就绪后发 WS `play_ready`（Day 3 C2 新增）→ 后端再发 `media_control: resume`。

```json
{
  "type": "media_info",
  "trace_id": "uuid-string",
  "media": {
    "media_id": "uuid-string",
    "title": "《穆桂英挂帅》选段",
    "genre": "opera",
    "url": "/static/media/opera/mu-gui-ying-001.wav",
    "duration_ms": 186000
  }
}
```

### 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | Literal `"media_info"` | ✅ | 帧类型 |
| `trace_id` | str (uuid) | ✅ | 全链路 trace |
| `media.media_id` | str (uuid) | ✅ | media_library 表主键 |
| `media.title` | str | ✅ | 前端展示用 |
| `media.genre` | Literal `"opera"` / `"film"` / `"radio"` | ✅ | **当前只 `opera`**（rules.py R-01~R-08 写死 intent="opera"）；`film` 等未来扩展时再加规则 |
| `media.url` | str | ✅ | 静态资源路径，nginx 托管 |
| `media.duration_ms` | int | ✅ | 毫秒，前端进度条用 |

---

## 3. tts_interrupt（不走 WS 媒体帧，走 WebRTC 下行 TTS 通道）

**原因**：`interrupt` 是打断 TTS 播报（不是打断媒体播放），pad 侧 TTS 走 WebRTC 下行音频轨，媒体走独立 audio 元素。两条通道互不干扰，所以 `interrupt` 不进 WS `/ws/signal`。

**实现**：Tool 内部调 TTS 会话的 `interrupt()`/`close()` 方法（TTS 适配层契约 C1 已冻），pad 侧 WebRTC 下行检测到 `tts_end` 或静默帧就自动截断。

**pad 侧实现**：WebRTC 下行有内置的 `speaking` 状态回调（aiortc + webrtc-internals），interrupt 后 backend 不再往下行轨写音频，pad 侧自然停止。**不需要额外 WS 帧**。

---

## 4. 帧序列示例（一次"换一段"完整链路）

```
① WS media_control: {action: "next"}
   ↓
② WS media_info:   {media: {title: "...", url: "...opera-002.wav", duration_ms: 186000}}
   ↓ pad 加载资源并就绪
③ WS play_ready:   {media_id: "xxx", ok: true}   ← Day 3 C2 新增的 C→S 类型（由 D 提 A2 冻结）
   ↓
④ WS media_control: {action: "resume"}
```

## 5. 帧序列示例（一次"打断播报"）

```
① WS media_control: {action: "interrupt"}
   ↓ 不走 WS，直接调 TTS 适配层 interrupt()
② WebRTC 下行音频轨：停止写入 → pad 侧 speaking=false → TTS 播放器截断
```

## 6. 实现责任方

| 工作 | 负责人 | 产出 |
|---|---|---|
| Tool 内部发 WS 帧 | **dev-B B4** | `play_opera` Tool 注册 + 内部 `_send_ws_frame()` 调 signaling 模块投递 |
| WS 帧投递接口（signaling 层） | dev-A A3（已就绪） | 复用 Day 3 C2 的 signaling 模块，只需 Tool 内部 import 投递 |
| pad 侧播放器按帧 shape 路由 | **dev-D** | TypeScript 接口对齐本文件 TypedDict + 事件分发 |
| rules.py tool_args 与本文件 action 枚举对齐 | **dev-A A5** | 已对齐（r2 版 R-01~R-08 全 `action: tool + tool_name=play_opera + action 字段`） |

## 7. 冻结口径

- **本文件 7 个 action 枚举值、2 个 WS type（media_control/media_info）、字段名不可改**
- **`payload` dict 可扩展**，新增字段走 payload 不扩顶层字段
- `stream_id` 必须是 WebRTC MediaStream 的 track id（pad 用来路由到正确音频轨），不能用 conversation_thread_id 或 elder_id

---

## 修订记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-11-11 | Q-D4 初始冻结：media_control 七动作 + media_info 元数据 + interrupt 走 TTS 通道不进 WS |
