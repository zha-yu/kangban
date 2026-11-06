# Task 04 · VAD 主链接入与遗留修复（A4，W4/W5 + a0）

> 状态：未开始　｜　版本：v1.0　｜　日期：2026-11-06
> 关联：docs/team_tasks/day04/README.md（分工总览与 C6 契约）｜docs/team_tasks/day04/a0-联调遗留问题修复.md｜docs/plans/day-04-3D与身份闭环.md §3.0 图③
> 上游依据：PRD v2.6 FR-DLG-04、AC-42（验收）｜技术设计文档 v1.5（实现）｜计划 T8/T9｜day-03-a5 交付物
> 变更记录见文末

## 职责与写作用域

- `app/api/audio.py`：a0-P1-2（真 ASR/TTS 接入替换 Mock）、a0-F-02（WAV 头拒收）
- `app/main.py`：a0-F-01（RequestValidationError 统一错误体 handler）
- `app/audio/vad.py`：webrtcvad 兜底接 utterance 主链（TEN-VAD 失效自动接管，不引第二种 VAD 形态）
- `app/core/metrics.py`：新增「唤醒→建连→ASR→路由→LLM→TTS」打点位（AC-42 数据来源）
- `docs/plans/task-a2-*.md`：契约变更记录（A2 委托项，只补记录不改契约内容）

**禁触**：signaling.py、voiceprints 域、frontend。

## 前置依赖

无跨人依赖。**执行顺序**：上午先清 a0 三问题（与四条工作线零耦合）→ 午后进 W4 VAD 主链接入 → 14:30 起在联调窗口用真音频验证兜底三场景。

## 交付物

1. a0 修复：`POST /api/audio/utterance` 消费 `build_asr/build_tts` 真适配器（假 audio_url 删除，TTS 不可用 → `audio_url=null` + 降级话术）；422 统一错误体 `{detail, code, trace_id}`；垃圾字节 → 422
2. VAD 主链：TEN-VAD（端侧主）→ webrtcvad（服务端兜底）→ 按住说话（降级二）三场景自动接管
3. metrics 打点：唤醒/建连事件进入 OTel trace（AC-42 ≤1.5s 口径的数据底座）

## 验收标准（自验后再进联调窗口）

```
真实说话 → text 与所说一致；audio_url 为 null 或真实可取地址（无 mock.mp3）
上传垃圾字节 → 422；缺字段/非 UUID → 错误体含 code/trace_id
TEN-VAD 不可用注入 → 服务端兜底切段仍出 utterance，链路不断
uv run pytest -q 全绿；ruff check app 通过
```

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：自 day04 README v1.0 §3/§6 拆出；a0 卡分派至本任务 |
