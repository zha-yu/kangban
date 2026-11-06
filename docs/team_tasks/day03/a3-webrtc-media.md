# A3 · WebRTC 媒体层

> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-05
> 关联：docs/team_tasks_day03/README.md（分工总览）
> 上游依据：day-03 计划 T3/T4（回声测试、全双工+VAD+打断）｜adhoc 方案 §1（主链路裁决：WebRTC 音频轨）｜PRD §2.2（AEC/打断验收）
> 变更记录见文末

## 职责与写作用域

- `app/audio/webrtc.py`：aiortc PeerConnection 管理、音频轨收发、事件循环绑定防护

**禁触**：signaling.py（A2 写作域，只 import 其消息类型）、frontend、ASR/TTS 实现（只调 C1 契约）。

## 前置依赖

09:30 后开工：需 C1（ASRResult）与 C2（WS 消息表）冻结。aiortc 依赖与 VAD spike Day 2 已就位。

## 交付物

1. PeerConnection 生命周期管理：经 C2 信令交换 SDP/ICE，ICE 配置支持 localhost 与 TURN 预留（`WEBRTC_ICE_SERVERS` 已在 Settings）
2. 收轨：opus 48k → 解码 → 16k PCM 16bit 单声道帧（60ms/帧）→ 喂 `ASRProvider.stream()`
3. 写轨：TTS 输出 PCM → 打包下发音频轨；**打断 = 立即停止写轨并清缓冲**（验收：点打断数字人立刻停）
4. **loop 绑定防护（头号故障源 P1）**：aiortc 全部协程显式绑定到启动时的事件循环；uvicorn `--reload` 与共享 loop 场景的崩溃防御单测
5. 会话状态：与 A2 断线检测联动（WS 死 → 关 PeerConnection + 清理）

## 验收标准

- `uv run pytest tests/test_audio_webrtc.py -q` 全绿：帧格式转换、loop 绑定、打断清缓冲、异常关闭不泄漏
- **回声测试（V3）**：浏览器说话 → 服务端日志看到连续 PCM 帧；TTS 写轨后浏览器能听到（本机 localhost 无需 coturn）
- 建连成功率：同机浏览器 10 次建连 ≥10 成功；SDP 交换全程带 trace_id

## 时间节点

09:30 放行 → 12:00 前收轨链路 → 15:00 前写轨+打断 → 14:30 起与 A4/A2 三方联调 → 17:00 收口提交。

## 联调对接点

- A4 的 offer 进来 → 本单元 answer/ice 下行（经 A2 信令）
- A1 的 C1 契约是唯一 ASR 接口；TTS PCM 来源同 A1
- 打断信号：A4 的 VAD speechstart → `asr_start` → 停写轨

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-05 | 初稿 |
