# A5 · 机动：VAD 兜底 + 降级链路 + 测试收口

> 状态：进行中　｜　版本：v1.0　｜　日期：2026-11-05
> 关联：docs/team_tasks_day03/README.md（分工总览）
> 上游依据：day-03 计划 T1（降级链路）/T5（降级话术）/T6（测试收口）｜PRD §2.2（降级不白屏）｜FR-TC-06（覆盖率）
> 变更记录见文末

## 职责定位

固定任务约占 70% 工时，剩余 30% 为机动弹性：**阻塞支援、公共组件（pytest fixtures、WS trace 测试工具）、临时任务**。任何成员阻塞超 30 分钟，A5 优先接手。

## 写作用域

固定：`app/audio/vad.py`、`app/api/audio.py`（降级路由）、`tests/`（Day 3 新增用例）、README 第 7 节验收记录
弹性支援：与被支援人协商，接管其写作域内具体任务（所有权不转移）

## 前置依赖

无（与 A1/A2 平行）。**产出 C3 契约**（utterance REST 形状）09:30 前定稿，A4 据此做降级模式。

## 交付物（固定部分）

1. C3 契约：`POST /api/audio/utterance`（multipart 16k PCM 整段 + device_token）→ `{"text": str, "reply": {...}}`，走设备 JWT 鉴权
2. `app/audio/vad.py`：webrtcvad 兜底（`WEBRTC_VAD_AGGRESSIVENESS` 已预留）——30ms 帧校验、speechstart/speechend 判定，供 A3/TEN-VAD 失败回落共用
3. 降级链路闭环（T1）：utterance 收音频 → ASR（C1 final）→ 回复组装 → TTS 合成返回
4. 攻击面测试（真实应用+真实数据库，禁止 mock）：伪造 device_token 打 WS/utterance → 4401/401 + 审计落库；越权 elder_id 传参 → 丢弃
5. 降级话术（T5）：三场景文案与触发逻辑——ASR 不可用/云 LLM 超时/工具熔断，统一走模板话术并提示人工
6. 收口（T6）：全量 pytest + 覆盖率核对（接口 ≥70%）+ README 第 7 节验收记录回填

## 验收标准

- `uv run pytest -q` 全绿；攻击面用例全部真实 DB 断言
- 降级链路：关掉 FunASR 容器 → utterance 返回明确错误话术而非 500/白屏（V1/V5）
- README 第 7 节新增 7.2「Day 3 验收记录」完整可追溯

## 时间节点

09:30 前冻结 C3 → 12:00 前 vad.py+utterance → 15:00 前攻击面测试+降级话术 → 17:00 收口会主导全量验证与提交。

## 联调对接点

- A4 降级模式消费 C3；vad.py 是 A3 与 A4 TEN-VAD 的公共回落
- 收口会主持：跑门禁、回填 README、汇总各卡变更记录

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-05 | 初稿 |
