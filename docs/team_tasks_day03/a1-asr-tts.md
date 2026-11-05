# A1 · ASR/TTS 适配层

> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-05
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
