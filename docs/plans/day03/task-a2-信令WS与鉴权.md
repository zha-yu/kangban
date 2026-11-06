> 状态：草稿　｜　版本：v1.0　｜　日期：2026-11-05
> 关联：docs/team_tasks_day03/a2-signaling-ws.md（A2 任务卡）｜docs/team_tasks_day03/contracts.md（C2 契约确认稿）｜docs/team_tasks_day03/README.md（分工总览）
> 上游依据：PRD §2.2 line 97–122、FR-TC-10 line 459、AC-40 line 521、§5.3（验收）｜技术设计文档 line 1016–1023「Day 3 · 语音通道」落地清单、line 225 audio 目录（实现）｜day-03 计划 v1.5 T2/V2/P6｜adhoc-20261105-音频传输与设备链路技术方案 v1.3 §三 M2｜接口契约_身份与权限.md E4/§5/自裁 S-08/S-23/裁决 A-02/A-03
> 变更记录见文末

---

# A2 执行计划：信令 WS 与鉴权（Day 3 · T2）

## 0. 单元定位与边界

A2 承担 Day 3 的 **T2 阶段**：`WS /ws/signal` 的首帧鉴权、心跳保活、WS 级 trace 注入、信令转发与断线清理。它是 A3（媒体层）与 A4（pad 前端）之间唯一的控制面通道：音频不走这条连接（走 WebRTC 轨），SDP/ICE/ASR 会话控制全走这条连接。

一句话边界：**A2 只做「连接、鉴权、转发、清理」四件事；不碰音频帧、不碰媒体层实现、不碰前端、不改鉴权内核。**

- 写作域（唯一所有权）：`app/audio/signaling.py`、`app/main.py`（仅追加路由装配的最小 diff）、`tests/test_audio_ws_auth.py`
- 禁触（违反即返工）：`app/core/auth*`（只调用既有函数，不改一行）、`app/audio/webrtc.py`（A3 域）、`app/audio/asr.py`/`tts.py`/`vad.py`（A1/A5 域）、`frontend/**`（A4 域）、`app/core/config.py`（Day 3 只读，新增字段由 A1 统一）、`alembic/**`（Day 3 不新增迁移，head 保持 0003）
- 分层硬约束：`app/audio` 不写库（AGENTS §2）；审计按 A-02/S-08 只落结构化日志，`audit_logs` 表 Day 9 建表后回填；依赖方向 `app/audio → app/core` 单向

## 1. 事实基线（只引用 PRD / 技术文档 / 已冻结计划）

| # | 事实 | 出处 |
| --- | --- | --- |
| F1 | 信令复用 FastAPI WebSocket，不新建信令服务器；承载 SDP offer/answer 与 ICE candidate 交换 | PRD §2.2 实现方案表（line 114） |
| F2 | Day 3 落地 `app/audio/signaling.py`：WS 承载 SDP/ICE、WS 级 trace_id 注入、设备 JWT 鉴权；不承载音频裸流 | 技术文档 line 1019、day-03 计划 §3.2 |
| F3 | 设备身份已就绪：E4 `POST /api/device-sessions` 签发 24h 设备会话 JWT，载荷 `{sub:"device:<id>", kind:"device", dev, tv:"<token_hash 前 16 位>", typ:"device"}`；解绑/轮换靠 `tv` 前缀常量时间比对回查失效 | 接口契约 E4/§5.1/S-21；Day 2 已交付 |
| F4 | 首帧鉴权（不用 query token，避免进访问日志）：连接后 300ms 内必须发 `auth{token}`；超时或验签失败 → `auth_fail` 后以 4401 关闭 + 审计 | adhoc M2 §3.1、C2 语义约定、day-03 计划 P6 |
| F5 | 心跳：客户端 20s 发 `ping`，服务端回 `pong`；45s 无任何帧判死清理；参数为初值，实测后回填留痕 | C2、adhoc M2 §3.2/§八 |
| F6 | trace：TraceIdMiddleware 对 `websocket` scope 透传不注入（`scope["type"] != "http"` 直接放行），WS 内 trace 由信令层自补 | `app/core/trace.py` line 39–41、day-03 计划 §2.1 G6 |
| F7 | `elder_id` 只从服务端设备绑定读取，客户端/模型传入一律丢弃 | AGENTS 禁区 9、C2 AuthOkMsg 注释 |
| F8 | WS 断开清理 ASR 会话/TTS 在途任务/帧缓冲，不释放 PeerConnection（A3 自行关闭） | C2 语义约定、adhoc M3 §4.3、A3 卡交付物 5 |
| F9 | 路径前缀：REST 统一 `/api`，**Day 3 的 `/ws` 另开一条** | 接口契约裁决 A-03 |
| F10 | 设备会话无 refresh 通道：E2 `POST /api/sessions/refresh` 只收人账号 refresh_token；设备只有 E4 换证（`app/api/auth.py` line 134–153 已核实） | 接口契约 E2/E4、代码核实 |
| F11 | 本日回复为写死话术，不接 LLM 路由/Agent/声纹；P95 ≤1.5s 属 Day 5 | day-03 计划 §0/§5 |

## 2. 前置依赖与任务依赖关系

```
Day 2 交付（已就绪）
  ├─ E4 设备换证 + resolve_principal 鉴权内核 + 审计日志模式   ──┐
  └─ tests/conftest.py 真实 DB 测试基建                          ├─▶ A2 全部任务
09:00–09:30 契约会（W0：A2 冻结 C2）                              ┘
  └─▶ W1 骨架 ─▶ W2 首帧鉴权 ─▶ W3 心跳 ─▶ W4 信令转发 ─▶ W5 断线清理 ─▶ W6 测试收口 ─▶ W7 登记与收口
A1 的 C1（ASRResult/ASREvent）──09:30 冻结──▶ W4 消费（事件转发 asr_partial/asr_final）
A3 在 09:30 放行，14:30 起 ──▶ 联调：A4 的 offer 经 A2 透传给 A3，answer/ice 经 A2 下行
A4 依赖 A2 的 refresh 核对结论（W7 产出）决定过期处理
```

关键先后约束：
1. **W0 必须最先完成**：C2 不冻结，A3/A4 无法开工（README 分工 §2 明确 A2 的 WS 消息表是它们的开发依据）。
2. W2 依赖 W1 的连接骨架；W4 依赖 W2（auth 通过才有后续消息合法性）与 C1 冻结。
3. W6 的「真实 offer/answer 往返」用例在 A3 就位前用进程内测试替身，14:30 联调后补真实链路断言。
4. W7 的 refresh 结论必须在中午前同步 A4（其交付物 7 的实现取向取决于此）。

## 3. 任务拆解

### W0 · 契约会：冻结 C2（09:00–09:30）

**前置**：无。
**步骤**：
1. 会前重读 `contracts.md` C2 全稿与 A3/A4 任务卡，收集两方对消息表的异议。
2. 会上逐条确认 13 个消息类型的方向与字段，当会裁决四个开放点（裁决结论记入 contracts.md 确认表）：
   - ① **心跳方向**：C2 现稿仅 C→S `ping` + S→C `pong`；adhoc M2 §3.2 另有「服务端每 20s 发 ping」表述。建议裁决：维持单向（客户端 ping、服务端 pong + 45s 判死），服务端不做主动探测；若 A4 需要下行活性检测再补 S→C ping。
   - ② **auth 后重复发 auth**：建议按非法消息处理 → `auth_fail code=invalid` + 4401（防止连接内换 token 旁路）。
   - ③ **同 device_id 二次连接**：建议踢旧留新（旧连接 close 后新连接接管注册表槽位），并在注册表操作上加锁；如无双开场景需求也可直接拒绝二连，会上定夺。
   - ④ **非法/未知消息类型**：建议回 `error code=internal`（脱敏文案）但不关连接，打 warn 日志；连续异常计数仅供观测，不做熔断（Day 3 无此要求）。
3. 会后把 C2 契约 **TypedDict 原样拷贝**进 `app/audio/signaling.py` 顶部（contracts.md 使用规则），群通告冻结。
**交付物**：contracts.md C2 状态改「已确认」；`signaling.py` 顶部契约块；四个开放点的裁决记录。
**自检标准**：
- [ ] signaling.py 顶部契约与 contracts.md C2 逐字段 diff 一致（字段名、Literal 取值、方向注释）
- [ ] A3/A4 在群里对冻结版本无异议（缺席视为接受——README §5.1）
- [ ] 任何后续改动均有群通告 + 任务卡变更记录（契约冻结纪律）

### W1 · 连接骨架与路由装配（09:30–10:00）

**前置**：W0 冻结。
**步骤**：
1. `app/audio/signaling.py`：定义 `WS_PING_INTERVAL_S = 20`、`WS_DEAD_AFTER_S = 45`、`WS_AUTH_TIMEOUT_MS = 300` 模块常量（带注释：初值出处 adhoc §六，实测后回填；Settings Day 3 只读，不动 config.py）。
2. 建 WS 端点 `@router.websocket("/ws/signal")`（**无 /api 前缀**，依据 F9）。
3. 定义连接上下文 dataclass：`trace_id / device_id / elder_id / room_id / last_frame_at / authed: bool / downstream: asyncio.Queue[dict] / disconnect_callbacks: list`。
4. 建连接注册表（`device_id → 连接上下文`，进程内字典 + 锁），供下行寻址与断线清理。
5. `app/main.py` 追加 `from app.audio import signaling` 与 `application.include_router(signaling.router)` —— 本单元对 main.py 的唯一合法改动，diff 控制在装配行。
**交付物**：可启动的空实现端点（accept 后仅回 echo 级占位，W2 替换）。
**自检标准**：
- [ ] `uv run pytest -q` 存量全绿（270+ 条不回归）
- [ ] `uv run uvicorn app.main:app --port 8000` 启动无报错，`/health` 200 且带 `X-Trace-Id`
- [ ] `git diff app/main.py` 只有装配行

### W2 · 首帧鉴权 + 审计 + trace 注入（10:00–12:00）

**前置**：W1；Day 2 鉴权内核（已就绪）。
**步骤**：
1. **鉴权调用**（复用，不新造）：对首帧 `auth.token` 组装 `Authorization: Bearer <token>` 调 `app/core/auth.py` 既有 `resolve_principal(authorization, session)`——其 `typ=device` 分支已按 S-19/S-21/S-25 完整验签并回查 `devices`。任务卡写的 `verify_device_token` 是口径简称，实际函数名以代码为准，**不改 `core/auth*` 一行**。
2. **300ms 首帧计时**：accept 后立即以 `asyncio.wait_for(receive(), WS_AUTH_TIMEOUT_MS/1000)` 等首帧；超时 → 发 `auth_fail{code:"timeout"}` → `close(4401)`。
3. **失败分类**（C2）：验签/回查不过 → `invalid`；`exp` 已过 → `expired`；`auth` 前收到其他任何消息 → `invalid`。三类均发 `auth_fail` 后 `close(4401)`。
4. **审计**：每次失败写结构化 warn 日志，字段沿用 Day 2 服务层审计模式（`trace_id / audit_action="ws_auth_failed" / actor="device:<id>"或"unknown" / target="/ws/signal" / result=<timeout|invalid|expired>`）；**不回显 token 内容**（S-23 ④）。`audit_logs` 表 Day 9 建后回填，不在本层写库（A-02/S-08）。
5. **trace 注入**：accept 时即 `new_trace_id()` + `set_trace_id()`（保证 auth 失败日志也有 trace 可查），成功后把 `device_id` 绑进连接上下文；连接关闭 `finally` 里 `reset_trace_id()`。此为 TraceIdMiddleware 不覆盖 WS 的自补（F6）。
6. **身份注入**：鉴权通过后用 `identity_scope()` 把 `Identity(kind="device")` 挂进本连接的处理上下文；`elder_id` 只从 `Identity`（库内绑定）读，进 `auth_ok{elder_id, room_id}`（F7）。
**交付物**：完整首帧鉴权流。
**自检标准**：
- [ ] 伪造 token → 收 `auth_fail` 且关闭码 4401；caplog 断言审计字段五项齐全、token 明文零出现
- [ ] 合法 token → `auth_ok`；`elder_id`/`room_id` 与 DB 绑定关系一致（**回查 DB 断言**，不是只看回包）
- [ ] auth 前发 `offer` → 4401；300ms 超时（测试中 monkeypatch 常量）→ `code=timeout` + 4401
- [ ] 解绑设备（走 E10）后旧 token 连 WS → 4401（AC-24 的 WS 面复现）

### W3 · 心跳保活（11:00 前后，与 W2 同窗口收尾）

**前置**：W2。
**步骤**：
1. 收到 `ping` 立即回 `pong`；收到**任何合法帧**刷新 `last_frame_at`（心跳计数不与业务消息混用——adhoc M2 §3.2）。
2. 判死任务：连接存活期间周期检查 `now - last_frame_at > WS_DEAD_AFTER_S` → 关闭连接（正常 close）并触发 W5 清理，打 info 日志 `ws_dead`（带 trace_id/device_id）。
3. 心跳参数不写 Settings（Day 3 config.py 只读）；模块常量留「初值，实测后回填」注释（adhoc §八）。
**自检标准**：
- [ ] 发 `ping` 即时收 `pong`
- [ ] monkeypatch 常量（如 0.2s 间隔/0.5s 判死）后静默连接被踢线，日志含 `ws_dead`
- [ ] 业务帧（如 `asr_start`）同样刷新活跃时间戳（持续发业务帧不误判死）

### W4 · 信令转发与 C1 事件下行（13:00–15:00）

**前置**：W2/W3；C1 已冻结（A1，09:30 产出）。
**步骤**：
1. **上行处理**：`offer`（校验 `sdp` 为非空 str）与 `asr_start`/`asr_end` → 投递给 A3 注册的处理回调；`ice` → 经下行队列原样透传回本连接（C→S 的 ice 在单服务端拓扑里由 A3 消费，S→C 的 ice 由 A3 投递）；`ping` → W3 已处理；`auth`（重复）→ 按 W0 裁决②拒。
2. **下行通道**：每连接一个 `asyncio.Queue[dict]` + 单一 writer 协程（绑定 uvicorn loop——P1 风险在 A2 侧的落点）；对 A3 只暴露两个公开面：**消息类型（TypedDict）+ 投递接口**（A3 卡口径「只 import 其消息类型」），A3 不得触碰 WebSocket 实例与内部状态。
3. **C1 事件转发**：`ASREvent.kind=PARTIAL → asr_partial{text}`；`FINAL → asr_final{text}`；`ERROR → error{code:"asr_unavailable", message:<脱敏降级话术>}`（错误映射为可读话术，不透出 `ASRError.message` 内部细节）。tts_text 由回复组装方（Day 3 为写死话术链路）投递，A2 只负责下发。
4. **会话 seam（与 A3 的接线，联调时确认）**：A3 负责把 WebRTC 轨收到的帧喂 `ASRProvider.stream()` 并把产出的 `ASREvent` 投给 A2 的下行队列；A2 负责 `asr_start/asr_end` 触发的会话状态机（含 30s maxSpeech 强制切段时的服务端配合——仅状态标记，切段判定在前端/A3）。该 seam 写进 C2 语义约定，若联调时双方认为职责倒挂更顺，走契约变更流程改。
5. 非法消息（缺 `type`/未知 `type`）→ 按 W0 裁决④处理。
**自检标准**：
- [ ] TestClient 连接发 `offer`（测试替身应答）→ 收 `answer`，sdp 原文一致
- [ ] `asr_partial`/`asr_final`/`tts_text`/`error` 四类下行消息 JSON 与 C2 定义逐字段一致
- [ ] 双连接隔离：A 连接的 answer/ice 不会出现在 B 连接（越线即缺陷）
- [ ] 非法消息不崩连接，且日志可查（warn + trace_id）

### W5 · 断线检测与状态清理（与 W4 同窗口）

**前置**：W4 的注册表与回调位。
**步骤**：
1. 断开路径统一收敛到一个清理函数，处理三来源：客户端主动断、心跳判死（W3）、服务端异常。
2. 清理清单（C2 语义约定）：ASR 会话在途标记（通知 A3 provider close）、TTS 在途任务取消、帧缓冲丢弃、注册表条目移除、`disconnect_callbacks` 逐个触发（A3 借此关 PeerConnection）、`reset_trace_id()`。
3. **不释放**：PeerConnection、音频轨（A3 域，F8）。
**自检标准**：
- [ ] 断开后注册表无残留（测试断言 registry 为空）
- [ ] 同一设备断开→重连→auth→offer 全流程可完整重复，无旧状态串扰
- [ ] 清理日志一条不少（含 trace_id），且 writer 协程无悬挂任务（`asyncio.all_tasks()` 断言）

### W6 · 测试收口 `tests/test_audio_ws_auth.py`（贯穿全天，17:00 前全绿）

**前置**：各阶段实现。
**说明**：沿用项目攻击面测试约定——**真实 app 实例 + 真实 DB + 单事务回滚，禁 mock 应用与数据库**；WS 客户端用 Starlette `TestClient.websocket_connect`（httpx 不支持 WS）。A3 处理器联调前未就位，offer 应答方用**进程内测试替身**承接（app/DB 仍真实）；此口径登记到任务卡变更记录。
**用例清单**（每条写操作回查 DB 后再断言）：
| # | 用例 | 断言 |
| --- | --- | --- |
| 1 | 不发 auth 直接发 offer | `auth_fail invalid` + 4401 + 审计 |
| 2 | 超时无首帧（monkeypatch 300ms→0.1s） | `code=timeout` + 4401 + 审计 |
| 3 | 伪造 token | 4401 + 审计五字段 + 日志无 token 明文 |
| 4 | 过期 token（签发后置 exp 过去） | `code=expired` + 4401 |
| 5 | 解绑设备旧 token | 4401（AC-24 WS 面） |
| 6 | 合法 token | `auth_ok`；elder_id/room_id 回查 DB 一致 |
| 7 | ping/pong 即时性 | 发 ping 收 pong |
| 8 | 心跳判死（monkeypatch 常量） | 踢线 + `ws_dead` 日志 |
| 9 | offer/answer 往返 | sdp 透传一致（联调后补真实 A3 链路版本） |
| 10 | 双连接隔离 | B 收不到 A 的下行 |
| 11 | 断线清理 | 注册表空、重连可重复、无悬挂任务 |
**自检标准**：
- [ ] `uv run pytest tests/test_audio_ws_auth.py -q` 全绿
- [ ] `uv run pytest -q` 全量不回归；`uv run ruff check app tests` 干净
- [ ] signaling 新增代码行覆盖率 ≥70%（FR-TC-06 接口口径）
- [ ] 手工：wscat 或浏览器完成一次 auth→offer→answer 透传演示（联调时留痕）

### W7 · 核对项登记与收口（12:00 前出结论，17:00 收口）

**步骤**：
1. **设备会话 refresh 核对**（任务卡指定核对项）：已核实（F10）——E2 refresh 只收人账号 refresh_token，设备会话无 refresh 通道。**结论：WS 层不做设备 token 续期；临期/过期统一回 `auth_fail code=expired`，客户端重走 E4 换证**。把结论登记到任务卡变更记录，**中午前同步 A4**（其交付物 7「过期回登录页」据此实现）。
2. 17:00 收口会：确认全量 pytest 与 ruff 通过；向 A5 提供 WS 端点路径与心跳实际参数（README 端点表与第 7 节验收记录由 A5 统一回填，避免公共文件冲突提交）；本卡变更记录补齐（C2 冻结、四个裁决、测试替身口径、refresh 结论）；当天一次性提交，只含自己写作域文件，Conventional Commits（建议 `feat(audio): WS /ws/signal 首帧鉴权与信令转发`）。
**自检标准**：
- [ ] 任务卡变更记录含：C2 冻结版本、四裁决、替身口径、refresh 结论四项
- [ ] A4 已确认收到 refresh 结论（群通告可查）
- [ ] `git status` 无他人写作域文件混入

## 4. 与其他单元的对接矩阵

| 对端 | 方向 | 内容 | 时机 |
| --- | --- | --- | --- |
| A1 | 消费 | C1 `ASREvent`（partial/final/error → WS 下行）；错误二分类映射 `error.code` | 09:30 C1 冻结后 |
| A3 | 互操作 | 上行 offer/asr_start/asr_end/ice 投递给 A3 回调；下行 answer/ice/ASREvent 经队列下发；断线回调通知 A3 关 PeerConnection | 14:30 联调 |
| A4 | 供给 | C2 全部消息定义（客户端信令实现）；refresh 结论（过期处理） | 09:30 冻结 / 12:00 前 |
| A5 | 供给 | WS 端点路径、心跳参数实际值（README 回填）；攻击面用例坐标 | 17:00 收口 |

## 5. 时间节点（对齐任务卡与分工总览）

| 时点 | 任务 | 里程碑 |
| --- | --- | --- |
| 09:00–09:30 | W0 契约会 | C2 冻结（本单元第一交付物） |
| 09:30–10:00 | W1 骨架 | 端点可启动、存量测试不回归 |
| 10:00–12:00 | W2+W3 | 鉴权+心跳完成（卡面节点：12:00 前） |
| 12:30–12:40 | 午站会 | refresh 结论已同步 A4 |
| 13:00–15:00 | W4+W5 | 信令转发完成（卡面节点：15:00 前） |
| 14:30–16:30 | 联调 | A4↔A2↔A3 三方；补真实往返用例 |
| 17:00–17:30 | W7 收口 | 全量 pytest + 一次性提交 |

## 6. 风险与红线

| # | 风险/红线 | 处置 |
| --- | --- | --- |
| R1 | **P6 token 泄露**：query token 会进访问日志 | 已定首帧鉴权消息方案；日志不回显 token（S-23 ④），测试用例 3 断言 |
| R2 | **P1 loop 绑定**（aiortc 头号故障，A2 侧变体） | 下行 writer 协程显式跑在 uvicorn loop；`--reload` 下 WS 断连属预期行为，验收不背锅 |
| R3 | 同设备重复连接导致注册表竞态 | W0 裁决③定夺；注册表操作加锁，测试覆盖 |
| R4 | WS 长连接 + contextvar 串号 | trace/identity 的 set/reset 严格成对（连接级 token，finally 兜底），用例 10/11 断言 |
| R5 | 契约冻结后 A3/A4 要求改 C2 | 只走「群通告 + 双方任务卡留痕 + 对方确认」流程，不合入未确认改动 |
| R6 | 红线：不改 core/auth*、不写库、不新增迁移（head 保持 0003）、不造第二鉴权入口、elder_id 只从服务端绑定读 | 违反即返工；提交前 `git diff` 逐文件自查 |
| R7 | 审计与 audit_logs 表（Day 9）的关系 | 本轮只结构化日志（A-02 裁决口径），不提前写库 |

## 7. 交付物汇总与质量要求

| 交付物 | 格式与质量要求 |
| --- | --- |
| `app/audio/signaling.py` | 顶部为 C2 契约 TypedDict（与 contracts.md 逐字段一致）；全量 type hint、I/O async、结构化日志每行带 trace_id；心跳/超时为模块常量并注明初值出处 |
| `app/main.py` 改动 | 仅装配行（import + include_router），diff ≤2 行 |
| `tests/test_audio_ws_auth.py` | 真实 app + 真实 DB，11 条用例全覆盖上表；写操作回查 DB；覆盖率 ≥70% |
| 任务卡变更记录 | C2 冻结、四裁决、测试替身口径、refresh 核对结论四项齐备 |
| 契约留痕 | contracts.md 确认表 + 冻结后变更的群通告记录 |

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-05 | 初稿：事实基线 F1–F11、W0–W7 拆解（含步骤/交付物/量化自检）、对接矩阵、时间节点、风险红线；登记设备会话无 refresh 通道的代码核实结论（E2 仅人账号，设备临期重走 E4） |
