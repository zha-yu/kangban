# Task 01 · 声纹身份闭环（A1，W2）

> 状态：未开始　｜　版本：v1.0　｜　日期：2026-11-06
> 关联：docs/team_tasks/day04/README.md（分工总览与 C4/C5 契约）｜docs/plans/day-04-3D与身份闭环.md §1.5/§2.1/§3.0 图②⑥
> 上游依据：PRD v2.6 FR-VPR-01/02/03（验收）｜技术设计文档 v1.5 §3.6 Day 4 批次（实现）｜计划 T3/T5/T7
> 变更记录见文末

## 职责与写作用域（部署形态已定：独立容器，2026-11-06 主理人裁决）

- `spikes/voiceprint/`：CAM++ spike（环境可跑 + 同/异人 cosine 分布实测）
- `deploy/voiceprint/`：**独立推理容器**（小型 FastAPI：`POST /extract` 音频 → 192 维 embedding；只做提取，比对与阈值判断在主服务进程内 numpy 完成，容器不装业务依赖）
- `docker-compose.yml`：追加 voiceprint 服务（端口 **7003**，CPU 限核，与 funasr 同款模式；本卡对 compose 的唯一合法改动）
- `alembic/versions/`：迁移 0004（`voiceprints` 两表）、0005（`personas.embedding_encrypted`，维度参数按 spike 结论）
- `app/services/voiceprint_service.py`：推理容器 HTTP 客户端封装（超时/503 二分类）+ 3 段注册编排 + Fernet 加密落库 + 原始音频即弃
- `app/services/identity_service.py`：候选恒为设备绑定长者（1v1）的级联判定（进程内 cosine ≥0.85）
- `app/api/voiceprints.py`：REST 三接口（samples/status/delete）
- README：voiceprint 端口/变量登记（沿 day03 A1 惯例）

**禁触**：signaling.py、llm/dialogue、frontend、他人写作域。**主服务 pyproject 不新增 torch**——重依赖只进容器；Settings 新字段（`VOICEPRINT_*`）报 A3 统一录入。

## 前置依赖

无跨人依赖。**契约已定稿**（[contracts.md](contracts.md) v1.0，晨会签字确认后开工）：本卡是 C4 冻结人、C5 提案人（五项开放点裁决已含），开工后第一时间把 C4/C5 原样拷入 `app/api/voiceprints.py` 与（配合 A2）signaling.py 追加段。

## 交付物

1. spike 结论：CAM++ 192 维容器内可跑、同/异人 cosine 分布实测数据（落 `spikes/voiceprint/`，12:30 午站会通报）
2. 推理容器 `deploy/voiceprint/` + compose 服务：`/extract` 正常返回 192 维、推理超时/失败 → 主服务侧 503；`docker compose up -d voiceprint` 健康检查通过
3. 迁移 0004/0005：UUID 主键、`(elder_id, version, sample_index)` 唯一约束、embedding 加密列；upgrade/downgrade 可逆
4. `voiceprint_service`：逐段上传（重复 409）、Fernet 加密、原始音频取完即弃（AC-36）
5. `identity_service`：每会话首轮强制校验 + 静默复检，进程内 cosine ≥0.85 命中，低置信/容器不可用 → 主动确认（候选集恒 1 位）
6. REST 三接口 + 统一错误体 `{detail, code, trace_id}`

## 验收标准（自验后再进联调窗口）

```
注册 3 段 → status=active；重复上传同 index → 409；停掉 voiceprint 容器注入 → 503 + 主动确认兜底
同人比对 cosine ≥0.85 命中；异人 <0.85；单/双人间候选集均恒 1 位
主服务 `uv sync` 后 venv 内无 torch（重依赖只在容器）
uv run alembic upgrade head && uv run alembic downgrade -1 可逆
库内 embedding 为密文；审计可查 DELETE 动作
uv run pytest -q 全绿；ruff check app 通过
```

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：自 day04 README v1.0 §3/§6 拆出 |
| v1.1 | 2026-11-06 | 主理人裁决：部署形态定**独立容器**（`deploy/voiceprint/` + 端口 7003，容器只做 /extract，比对在主服务进程内），主服务 pyproject 不新增 torch；写作域与验收同步更新 |
| v1.2 | 2026-11-06 | thread 生命周期修正：`verify_session_identity` / `open_thread_from_confirm` / `open_thread_as_generic_on_timeout` 在 INSERT 新 thread 前先结束该设备所有未结束 thread，避免重连/confirm/超时留下多个 active thread；新增 `tests/test_identity_thread_lifecycle.py`（FakeSession 顺序测试，不依赖 DB） |
| v1.3 | 2026-11-06 | 换人检测修正：`recheck_session_identity` 在已识别会话低于阈值时结束旧 thread 并返回 `confirm/low_confidence`（不再静默继续个人上下文）；generic 会话命中绑定长者时升级 voice 并开新 thread；新增同人/换人/generic 升级/generic 未命中/未绑定 5 类复检测试 |
| v1.4 | 2026-11-06 | `session_seq` 落地：personal thread 在 INSERT 前同事务 `Elders.session_seq + 1 RETURNING`，thread_id 改为 `(elder_id, session_seq)`；generic 无 elder，session_seq=0、thread_id 用 UUID hex；`IdentityDecision.session_seq` 回传 A2；补 FakeSession 序号断言 |
