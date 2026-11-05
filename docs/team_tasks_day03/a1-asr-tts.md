# A1 · ASR/TTS 适配层

> 状态：已完成（N8 真链路未达成，见文末收口记录）　｜　版本：v1.1　｜　日期：2026-11-05
> 关联：docs/team_tasks_day03/README.md（分工总览）
> 上游依据：day-03 计划 T1（asr/tts 协议+工厂+置信度进 trace）｜adhoc 方案 §M1 侧链｜技术文档 Day 3 清单
> 变更记录见文末

## 职责与写作用域

- `app/audio/asr.py`：`ASRProvider` 协议 + FunASR 适配器 + Mock 实现 + 云 API 骨架
- `app/audio/tts.py`：`TTSProvider` 协议 + edge-tts 兜底实现 + 云 API 骨架
- `app/audio/__init__.py`：工厂 `build_asr()/build_tts()`，按 Settings 分派
- `docker-compose.yml`：追加 funasr 服务（端口 7001）
- README 第 6 节：funasr 端口/变量登记

**禁触**：signaling/webrtc/vad/utterance、frontend、他人写作域。Settings 只读（字段已预留：`asr_*` 7 项、`tts_*` 7 项）。

## 前置依赖

无（本单元是全组上游）。**产出 C1 契约**（ASRResult/ASREvent/错误二分类）供 A2/A3/A5 消费，09:30 前定稿。

## 交付物

1. C1 契约定义（dataclass，含 `confidence: float|None`——FunASR 占位 `null` 不造假值）
2. FunASR 适配器：`subprotocols=["binary"]`、`ping_interval=None`（禁 ping 帧）、等终稿 15s 超时、`is_speaking/is_end` 收尾、preRoll 补发
3. 错误二分类：`ASRUnavailable`（可退避重试 ≤2）/`ASRProtocolError`(不可重试直接降级)
4. Mock 实现与工厂分派（`ASR_PROVIDER=mock|funasr|cloud`）
5. 云 API 适配器骨架（接口形状 + TODO，等教师 key）

## 验收标准（自行验证后再交联调）

- `uv run pytest tests/test_audio_asr.py -q` 全绿：mock 路由、confidence=null 语义、错误二分类、工厂分派
- FunASR 连通 spike：容器起后真实语音出字（教师模块同款验证，录屏/日志留痕）
- TTS：edge-tts 整段合成 16k PCM 输出（非流式，知会全组首包预算受限）

## 时间节点

09:30 前冻结 C1 → 12:00 前 FunASR 适配器+容器跑通 → 15:00 前云骨架+工厂完成 → 17:00 收口提交。

## 联调对接点

- A3 喂 16k PCM 帧给 `ASRProvider.stream()`；partial/final 经 A2 转发
- A2 的 `tts_text` 字幕与 A3 下行写轨消费 TTS 输出
- A5 的 utterance 用 `final` 结果组装响应

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-05 | 初稿 |
| v1.1 | 2026-11-05 | 状态转「已完成」；补收口记录 |

## 收口记录（2026-11-05）

**提交**：`a4cb857` `feat(audio): Day3 A1 ASR/TTS 适配层`（17 files, +2593/−4）
**计划件**：`docs/plans/task-a1-ASR与TTS适配层.md` v1.2、`docs/plans/task-a1-DAG.md` v1.2

### 交付对照任务卡

| 交付物 | 状态 | 落点 |
| --- | --- | --- |
| ① C1 契约冻结 | ✅ | `app/audio/asr.py` 行 39–92，逐字对齐 `contracts.md` §C1；09:30 前交付 A2/A3/A5 |
| ② FunASR 适配器 | ✅（代码） | `FunASRWSProvider`：握手 2pass、`subprotocols=["binary"]`、`ping_interval=None`、`asyncio.timeout(15)`、`is_speaking/is_end` 收尾 |
| ③ 错误二分类 | ✅ | `ASRUnavailable`（可退避）/ `ASRProtocolError`（直接降级），各有用例 |
| ④ Mock 与工厂分派 | ✅ | `build_asr()` / `build_tts()`，`mock\|funasr\|cloud` 与 `mock\|edge\|cloud`，未知值 warning 归一到 mock |
| ⑤ 云 API 骨架 | ✅ | `CloudASRProvider` / `CloudTTSProvider`，等教师 key |

### 实测

- `ruff check .` → All checks passed
- `ruff format --check .` → 49 files already formatted
- 单点：`tests/test_audio_asr.py` **33 passed**、`tests/test_audio_tts.py` **38 passed**
- 全量（带库一次性容器）：**337 passed, 4 skipped**（skip 为容器无 ffmpeg 的真转码用例）

### 未达成

**N8 FunASR 真链路**：镜像两轮共 41 分钟未拉全（12 层中 3 层解压完成），`ws_probe.py` 未对真容器跑过。
已按 P-A1-1 在 `spikes/funasr/README.md` §4/§6 如实记录，后续 5 步待办也写在那里。**未伪造任何出字结果。**

### 给下游的三条硬话

1. `ASRProvider.stream()` 必须 `async for` 读到自然结束；提前退出用 `contextlib.aclosing`。
2. `FINAL` / `ERROR` 恒为最后一个事件。
3. `confidence` 在 FunASR 链路恒为 `None`，只进 trace，不参与业务决策。
