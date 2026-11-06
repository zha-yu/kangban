# Task 03 · 3D 展示后端（A3，W1）

> 状态：已完成　｜　版本：v1.2　｜　日期：2026-11-06
> 关联：docs/team_tasks/day04/README.md（分工总览与 C7 契约）｜docs/plans/day-04-3D与身份闭环.md §1.1/§2.1/§3.0 图④
> 上游依据：PRD v2.6 FR-3D-01/02、AC-15（验收）｜技术设计文档 v1.5 §1.4（实现）｜计划 T1/T2
> 变更记录见文末

## 职责与写作用域

- `app/core/config.py`：本日 Settings 新字段唯一录入人（汇总 A1 `VOICEPRINT_*`、A2 `LLM_*`/`ENCRYPTION_KEY`、本卡 `AVATAR_*`/`PUBLIC_BASE_URL`）
- `app/api/pad.py`：`GET /api/pad/avatar-config`（elder_id 服务端解析，客户端传入丢弃）
- `deploy/assets/`：AvatarSample_A 预置 + fallback PNG
- `deploy/nginx*.conf`：`/avatars/` 静态托管（不经后端转发，PRD §2.1 口径）
- `scripts/seed*`：persona 数据 + avatar_url 取值链数据
- README：端口/变量/资产登记

**禁触**：signaling.py、services、迁移文件、frontend。

## 前置依赖

无跨人依赖。**C7 已定稿**（[contracts.md](contracts.md) v1.0，晨会签字确认），本卡为 C7 冻结人，开工后拷入 `app/api/pad.py`；前端挂载开发可立即启动，不受晨会约束。

## 交付物

1. Settings/env/seed：`avatar_url` + `fallback_image_url` + `rotation_y_offset` + `version`；三层取值链 persona → Settings → 前端内置
2. nginx `/avatars/` 静态托管（VRM 体积口径 ≤15MB，AvatarSample_A 约 10–15MB 满足）
3. avatar-config 接口：设备 JWT 鉴权，响应带 `version`（运营换形象只 UPDATE 一行，前端缓存失效）

## 验收标准（自验后再进联调窗口）

```
curl 带设备 JWT → 200；不带/伪造 → 401
三层取值链逐层降级正确（删 persona 值 → Settings 兜底 → 前端内置）
.vrm 经 nginx 可拉取且 Content-Type 正确；>5s 场景由前端竞速兜底（AC-15）
改 persona.avatar_url → 响应 version 变化，config 改数据即换形象、零代码改动
uv run pytest -q 全绿；ruff check app 通过
```

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.1 | 2026-11-06 | 澄清「禁触 services」的口径并记录两项裁决：①**允许 A3 新建** `app/services/persona_service.py`（day-04 README:65-67 显示 `app/services/` 本就有 A1/A2 多个 owner，禁的是互相修改既有文件，不是禁止新增）；约束为该文件只读、不 import 任何既有 service。②nginx **不进** compose，本卡只交 `deploy/nginx.conf` + 一次性容器验证，正式集成留 Day 10。详见 `docs/plans/day04/a3-3D展示后端.md` A-01/A-02 与 v1.4 |
| v1.2 | 2026-11-06 | 交付完成，状态改「已完成」。交付物：`app/services/persona_service.py`、`app/api/pad.py`、`app/main.py` 挂载、`scripts/seed_personas.py`、`deploy/nginx.conf`、`deploy/assets/`（README + `.gitkeep` + 10.7MB VRM）、`tests/test_pad_avatar.py`、README §6.1/§6.2/§7.3。验收证据见 README §7.3 与本计划 v1.5 |
| v1.0 | 2026-11-06 | 初稿：自 day04 README v1.0 §3/§6 拆出 |
