> 状态：进行中（D1 代码已收口，待 X1 联调与实测）　｜　版本：v1.3　｜　日期：2026-11-12
> 负责人：**dev-D（唯一）**　｜　任务包：**D · 长者端语音补全与评测门禁**　｜　容量：**19h ＝ 2.375 人日**（Laya/Jev 对比裁减后）
> 关联：docs/team_tasks/day09/day09-任务总览与DAG.md v1.0（§4.4 分工、§5 对接点、§6 写域）｜docs/team_tasks/day09/contracts.md（X1/X8/X9/X10 提供与消费）
> 上游依据：PRD line 247（FR-UI-01 降级须进路由、单次最长 120 秒）、line 257（FR-UI-11 唤醒词 P0）、line 336（FR-MED-06 反向外呼＋`rtc_failed`）、line 254（FR-UI-08 撤销）、line 401（FR-ALT-06 SOS）、line 405–412（FR-RPT-01~04）、line 531–536（固定测试集 ≥45/50）、line 547（路由 ≤200ms）、line 95（家属端正文 ≥16px）、line 286（FR-VPR-04）、line 489/497/505/519/523（AC-08/20/23/37/41/42）、line 590（不得裁减：反向外呼与唤醒词）｜技术设计文档 line 1173（声纹 FAR/FRR 表、Laya 与 Chinese-Jev 对比报告、接口覆盖率 ≥70%）、line 1182（`/metrics` 口径由 C 承接）
> 遗留承接：**F01（pad 回执）F02 F03（pad SOS）F10（family 第五类）F15 F16 F17 F21（素材）F22（夹具 gold）F24 F28 F29 F30（播放）F31** —— 13 项
> 变更记录见文末

---

# dev-D · Day 9 任务书 · 长者端语音补全 ＋ 评测门禁 ＋ 家属端

## 0. 你的边界（先读这条）

### 0.1 写域仅限（越界即返工）

```
app/api/audio.py
app/api/reports.py
app/api/family.py                     （增量）
app/audio/**                          （含新增 outbound.py、signaling.py 收帧）
frontend/src/pad/**
frontend/src/family/**
frontend/public/**                    （KWS 资产布局）
tests/eval_gate.py
eval/fixtures/day7_intents.jsonl      （gold 修正）
eval/reports/**
.githooks/**
scripts/**
static/media/**                       （不入库，重建脚本须可跑）
对应 tests
```

### 0.2 禁触

`app/models/**`、`alembic/**`（A）、`app/services/**`（A/B/C 各自持锁）、`app/graphs/**`、`app/scheduler/**`、`app/agents/**`、`app/router/**`、`app/main.py`（**B 独占**）、`app/core/**`（A/C）、`frontend/src/console/**`（C）、`eval/fixtures/injection.jsonl`／`tests/test_injection.py`（**B4**）、`tests/test_gates.py`（**A2**）。

**特别注意**：`app/api/__init__.py` 的 ROUTERS 你**本日无需追加**（新端点落你已持锁的 `audio.py`／`reports.py`／`family.py` 内）。若确需新文件，提需求给 A/C 按顺序追加。

### 0.3 高风险文件（AGENTS §5：先出方案再落代码＋人工逐行读 diff）

`app/audio/**`（WebRTC 建连与媒体，鉴权相邻）、`app/api/audio.py`（上传降级鉴权）。**这两处必须先出方案经确认后才落代码。**

### 0.4 角色口径

本包**不含**独立测试/产品/设计角色工作。`eval_gate.py` 与夹具是**评测门禁的开发交付物**（TD/PRD 明列的实现项，day08 D5 同口径）；`eval/reports/**` 是**跑出来的数据报告**，不是产品文档；pad/family UI 按既有页面与 PRD 既有要求实现，不产生新设计工作。

### 0.5 两项「不得裁减」在你包里

PRD line 590 明列**不得裁减**：「WebRTC 全双工对话与**反向 WebRTC 外呼**（含降级模式）」、「**唤醒词激活**（P0）」。对应 **D2（F17）**与 **D1（F16）**——**这两项无论容量多紧都不顺延**（day08 审查计划 Q1 曾建议顺延，本编排已否决，依据即此条）。

---

## 1. 节点 D1 · 回执帧 ＋ pad 撤销 ＋ SOS 真实态 ＋ KWS 目录（4h）

**前置**：N0（X1 帧形状冻结）。**无其他硬前置，D9 上午第一个开工（C3 在等 X1）。**
**对接**：**提供 X1**（回执帧生产者＋信令收帧）给 C3。

### 1.1 现状实测（2026-11-12 取证）

- `frontend/src/pad/pages/MedicationCard.tsx:18 onRendered?: () => void`，`:47-50` 挂载即调 `onRendered?.()`；**但 `frontend/src/pad/components/TaskPanel.tsx:31-37` 渲染 `<MedicationCard>` 时只传 `key/data/onExit/onSubtitle/onAnswer`，不传 `onRendered`**。全仓 `card_rendered|tts_finished` 仅 5 处命中，全是 docstring 与可选回调 → **无真实回执生产者**。
- `MedicationCard.tsx:121 onClick={() => setState("pending")}` —— 撤销**纯前端回滚**；后端 `app/api/pad_undo.py:50-77` **已实现且有测试**（缺口只在前端）。
- `frontend/src/pad/pages/SosResult.tsx:83` 硬编码「负责您的护工和家属已经收到通知」；`:95` 硬编码 `["求助已记录","护工已通知","家属已通知"]`，**只要 `status==="sent"` 就渲染**，与实际通知状态无关。`:126-128`「这里不是急救通道」声明**已合规，保留**。
- `frontend/src/pad/kws/kws-detector.ts:14 KWS_ASSET_PREFIX = "/kws/"`，`:74` 加载 `prefix + "sherpa-onnx-kws.js"`，`:79 locateFile`，`:82` main.js；实际文件在 `frontend/public/kws/kws/{sherpa-onnx-kws.js, sherpa-onnx-wasm-kws-main.js/.wasm/.data}` —— **多一层目录**；`scripts/build-kws-wasm.md:28-29` 规定平铺 `frontend/public/kws/`；`:129-131 catch → warn + return null` **静默降级**；另含 `__MACOSX/` 垃圾目录。

### 1.2 要做什么

1. **F01 回执帧（X1）**：`TaskPanel.tsx:31` 补传 `onRendered`，回调内按 contracts.md **X1** 发 `client_receipt` 帧；`app/audio/signaling.py` 收帧 → 校验 `task_id` 归属当前设备绑定长者 → 调 C 的 `mark_notification_delivered`。**幂等键 `(notification_id, receipt_kind)`**，重复回执返回原结果。**`elder_id`/`device_id` 一律服务端注入，客户端传值丢弃**。
2. **F24 撤销调后端**：`MedicationCard.tsx:121` 改调 `app/api/pad_undo.py` 既有端点，**成功后再回滚 UI**；失败显示可重试。
3. **F03 SOS 真实态**：`SosResult.tsx` 三格状态改由**后端返回的真实通知状态驱动**（`sent` 只代表事件＋工单已生成）。文案分级：事件已生成 → 「求助已记录，正在通知护工」；**仅当 X1 回执确认后才显示「已通知」**。保留「非急救通道」声明与「我说错了（补充说明）」入口（AC-26：红色事件不可长者一键撤销）。
4. **F16 KWS 目录**：资产移到**平铺 `frontend/public/kws/`**（优先改资产布局以对齐 `build-kws-wasm.md`，避免脚本与代码长期不一致）；删 `__MACOSX/`；加载失败**不再静默**——页面显式提示「唤醒不可用，请按住说话」＋上报可观测降级标记。

### 1.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `TaskPanel.tsx` 补传 `onRendered`＋回执发送 |
| 2 | `app/audio/signaling.py` `client_receipt` 收帧＋归属校验＋调 C 函数 |
| 3 | `MedicationCard.tsx` 撤销调后端＋失败可重试 |
| 4 | `SosResult.tsx` 状态驱动文案 |
| 5 | `frontend/public/kws/` 平铺资产＋删 `__MACOSX/`＋显式降级提示 |
| 6 | X1 联调证据（与 C3）＋唤醒延迟实测 20 次 |

### 1.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| pad 渲染卡片 | DB 对应行 `status='delivered'` | PRD:387／AC-23 |
| 不渲染卡片 | 保持 `pending`（三次扫描后 `failed`，C3 侧） | AC-23 |
| 重复回执 | 返回原结果、`duplicate=true`，**不重复写 `sent_at`** | X1 幂等 |
| 点撤销 | DB 终态**回退**；后端拒绝时 UI **不误显示已撤销** | FR-UI-08 |
| 断开通知出口后按 SOS | 页面**不得出现「已通知」** | F03／AGENTS §4.4 |
| SOS 页 | **无红色事件撤销入口**；「我说错了」补充说明可用、事件保持 open | AC-26 |
| KWS 三资产 | `sherpa-onnx-kws.js`／`.wasm`／`.data` **全 200** | F16 |
| 说「小伴」 | 唤醒 **≤1.5s**，**20 次中 ≥18 次**达标 | AC-42／PRD:257 |
| 故意改错资产路径 | 页面**显式降级提示**（不再静默 return null） | F16 |
| **退房被动链路（M5.4 联调，配合 A3/C6）** | A3 触发退房后：pad 持旧设备 token → **401 且回登录/未授权态**（既有 `pad/api.ts:58-63` 口径，**不需新代码，只验证**）；原绑定家属查该长者日报 → **403 且「无权查看该长者的数据」/「未绑定长者」空态**（`family/api.ts:265`、`HomePage.tsx:91`，family 侧与 D4 同场确认）。**D 负责前端表现断言，不改后端** | FR-ADM-09／AC-24／FR-RPT-02 |

```bash
pnpm --dir frontend exec tsc --noEmit
pnpm --dir frontend build
# 浏览器 Network 面板核对三资产状态码；唤醒 20 次记录延迟
```

**注意**：`frontend/public/kws/` 被 `.gitignore:89` 排除，**符合 AGENTS §4.11（不提交大文件）**，本项**不改 gitignore**；须保证 `scripts/build-kws-wasm.md` 流程在新克隆机器可重建。

**CP-D1**：报告 9 条判据实测＋三资产状态码＋唤醒 20 次延迟表。**须 D10 12:00 前与 C3 完成 X1 联调。**

---

## 2. 节点 D2 · 降级进路由 ＋ 反向外呼 ＋ ICE 配置（7h）

**前置**：D1（复用同一信令与回执通道）。**软依赖**：B3 路由入口稳定（**可先做外呼与 ICE，不等待**）。
**对接**：消费 X8（`rtc_failed` 枚举，A1 提供）；消费 B3 的编排入口。**🔓 2026-11-12 17:17 协调人宣布 A1 已提前交付并通过验收 → D2 的 F17（`rtc_failed` 落库）解除写库阻塞；⚠️ 工作区 `alembic` 当前仅至 0019、无 `0020_*` 提交，D2 写库前须先 `alembic upgrade head` 且 `\d medication_events` 确认 `failure_reason` CHECK 已含 5 值，否则写 `rtc_failed` 触发 CHECK 违例。**

### 2.1 现状实测

- `app/api/audio.py:311-312`：
  ```python
  # --- ④ Day 3 写死回复话术（大纲课时 3.4 明确）---
  reply_text = f"您说的是『{asr_text}』，我记下了。"
  ```
  上限 **30s**（`audio.py:78`），PRD 要求 **120s**。注释以**大纲**为权威依据，**违反 AGENTS 前置铁律第 1/6 条**。
- `app/audio/` 实存 `__init__.py / _observe.py / asr.py / fixtures/ / signaling.py / tts.py / vad.py / webrtc.py` —— **无 outbound.py**。`app/audio/__init__.py:4` docstring 列有「反向外呼」；全仓 `反向外呼|outbound_call|reverse` 仅命中该 docstring 与 `history_service.py:605` 的 `views.reverse()`。`app/audio/webrtc.py:875` **只被动应答 offer**。
- `frontend/src/pad/task-channel.ts:7,39` 自述「主路径未落地/尚无自动接听能力」。
- `rtc_failed` 在 `app/` 与 `frontend/src/` **零命中**；`app/models/medication.py:129-137` CHECK 无该值（**A1 迁移 0020 扩**）。
- `frontend/src/pad/realtime.ts:365 const pc = new RTCPeerConnection();` —— **无 iceServers**；`config.py:166-169` TURN localhost 默认被 `webrtc.py:243 _is_loopback_ice_url` 丢弃；compose 无 coturn。

### 2.2 要做什么

1. **F15 降级进路由**：删除写死回复，ASR 文本走与 WS FINAL **同一条编排入口**（`user_text` 同路，参照 `MedicationCard.tsx:75` 既有口径）；上限 **30s → 120s**；**删除以大纲为依据的注释**，改注 PRD line 247。
2. **F17 反向外呼（不得裁减）**：新建 `app/audio/outbound.py`——服务端主动发 offer，**复用既有信令 WS，不新建信令服务**（AGENTS §1）；`frontend/src/pad/task-channel.ts` 落地**自动接听**主路径；呼叫失败落 **`failure_reason='rtc_failed'`**（依赖 X8）；触达失败分类与 AC-20 分流对齐（B4 侧）。
3. **F29 ICE 配置**：在 **`app/api/audio.py`**（你持锁）增 ICE 配置下发端点，前端从该端点取 ICE 配置（含 TURN）；**TURN 凭据从环境注入，不硬编码**（AGENTS §4.2）；**coturn 服务本体归 Day 10 compose**，本项只做前端与配置下发。

### 2.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `app/api/audio.py` 降级进路由＋120s＋注释依据改 PRD |
| 2 | `app/audio/outbound.py`（新）服务端主动外呼 |
| 3 | `frontend/src/pad/task-channel.ts` 自动接听主路径 |
| 4 | `frontend/src/pad/realtime.ts` ICE 配置接入＋下发端点 |
| 5 | `rtc_failed` 落库路径（与 B4 的 AC-20 分流对齐） |
| 6 | AC-41 端到端证据＋降级三用例证据 |

### 2.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| 上传「今天天气怎么样」 | **真实天气工具结果**，不是「您说的是…」 | FR-UI-01／AC-01 |
| 上传急症语料 | 安全一票否决＋人工告警，**不进大模型** | AC-04 |
| 上传 121s 音频 | **422** | PRD:247（120s 上限） |
| `grep -n "大纲" app/api/audio.py` | **零命中** | AGENTS 前置铁律 |
| 服务端发起外呼 | pad **自动接听** → **双向音频通** | FR-MED-06／AC-41 |
| 呼叫失败 | `failure_reason='rtc_failed'` **落库** | PRD:336 |
| 外呼失败降级 | 降级页面播报（PRD:575 Day 7 完成定义口径） | PRD:590 含降级模式 |
| `RTCPeerConnection` | **含 iceServers** | F29 |
| TURN 凭据 | **不出现在前端源码与日志** | AGENTS §4.2 |
| 信令通道 | 仍 `/ws/signal`，**未新建信令服务** | AGENTS §1 |

```bash
uv run pytest tests/ -q -k "audio or outbound or webrtc"
pnpm --dir frontend exec tsc --noEmit
grep -rn "rtc_failed" app/ frontend/src/    # 期望有命中（此前零命中）
```

**CP-D2**：报告降级三用例真实应答＋121s 边界＋外呼双向音频证据＋`rtc_failed` 落库行。**D2 完成即 M5.3 判据达成。**

---

## 3. 节点 D3 · 门禁硬线 ＋ 七层归因 ＋ 夹具判黄 ＋ hooks（4h）

**前置**：**软依赖** B3/B4 路由改进（**本体可先建，不等待**）。
**对接**：**提供 X9**（门禁硬线与归因分工）给全员；消费 B3 的 F22 判黄改动。

### 3.1 现状实测

- `eval/reports/day8_baseline_lock.json`：`normal_correct 41 / normal_total 50 / **82.0%**`、`latency_p50_ms 241`、`latency_p95_ms **297**`、`failing_ids` **15** 条、`layers {system_one: 83, rule: 37}`。
- `tests/eval_gate.py:72 SENSITIVE_FLOOR_PCT = 95.0`（唯一 PRD 硬线）；`:546-563` 闸门 B **只与锁文件对比**；`:577-601` 闸门 D 只要求「本次失败 ⊆ 锁内失败」，注释明写「要求全对等于把门禁线抬到比基线更高…**那是改验收，不是 D 的权限**」；`:536` 闸门 A；`:565-575` 闸门 C。**没有任何闸门校验 PRD ≥45/50 与 ≤200ms**。
- `tests/eval_gate.py:85 ATTRIBUTION_LAYERS`、`:205-240`：**ASR／召回／LLM／Tool 四层为硬编码字符串「无数据」**，只有分类器/规则层/编排算真实值。
- `eval/fixtures/day7_intents.jsonl` **110 条**（普通 50／敏感 20／边界 20／模糊 20）；boundary 20 条（negation/history/other_person/hedge 各 5）**全部 `gold_intent="other"`、`is_sensitive=False`**，与 PRD line 272 判黄口径不一致。
- `.githooks/pre-push` 只跑 `eval_gate.py --self-check`（R1② 裁决：需 torch＋~60s），且 `if [ -z "$py" ]; then … exit 0` 静默跳过；**`git config --get core.hooksPath` 返回 exit 1（未设置）**。
- **ASR 置信度已埋点**（`app/audio/_observe.py:69`、`app/models/decision.py:119`、`app/services/decision_log_service.py:87/:114`）；`_observe.py:59` 注明 funasr 源 confidence 恒 None 属**数据源限制**——**不是「未埋点」**。

### 3.2 要做什么

1. **F02 新增闸门 E（PRD 硬线）**：`normal_correct/normal_total >= 0.90`、`latency_p95_ms <= 200`、注入拦截 100%（消费 B4 的 `test_injection` 结果）。**与 B/D 并列且独立，不改 B/D 现有语义与权限**。
   - **权限说明（X9，避免争议）**：E 校验的是 **PRD 明文硬线**，**不高于 PRD**，故**不属**原注释禁止的「私自加严」（该注释反对的是「要求全对」）。
   - E 失败 → 打印未达标项与差值、**退出码非 0**；写锁文件须显式 `--allow-below-prd` ＋留痕理由。
2. **F31 七层归因接真实数据**：ASR／分类器／规则层／召回／LLM／Tool／编排 **七层均须真实值或带原因的空值**，**删除硬编码「无数据」**。ASR 层接既有埋点；funasr 源 confidence 恒 None 须**如实标注原因**。
3. **F22 夹具 gold 修正**：boundary 20 条 gold 由 `other`/`is_sensitive=False` 改为**判黄复核**口径（依据 PRD line 272）。**属向 PRD 对齐，非改验收**。
4. **F02 hooks**：`.githooks/pre-push` 无 python 时**报错退出**而非 `exit 0`；真门禁接线（**阻塞模式需主理人确认，总览 Q2**；默认按 AGENTS §3 接为阻塞）。启用 `git config core.hooksPath .githooks`（环境项，需用户执行）。
5. **接口覆盖率 ≥70%**（TD:1173）：产出覆盖率报告。

### 3.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `tests/eval_gate.py` 闸门 E＋七层归因真实数据 |
| 2 | `eval/fixtures/day7_intents.jsonl` boundary gold 修正 |
| 3 | `.githooks/pre-push` 真门禁接线＋无 python 报错 |
| 4 | 覆盖率报告（接口 ≥70%） |
| 5 | 闸门 E 对当前基线的**真实判定输出**（预期红） |

### 3.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| 以当前锁文件跑闸门 E | **必须失败**，打印 `82% < 90%`、`297ms > 200ms` 两项差值 | PRD:531-536／547 |
| 构造 45/50＋p95 195ms 假锁 | E **通过** | 判据可验证 |
| 七层归因 | **无硬编码「无数据」**；空值须带原因 | F31／PRD §5.4 |
| boundary gold 改判黄后 | 敏感集 **≥19/20 不回退** | PRD:531-536 |
| 无 python 环境跑 pre-push | **退出码非 0** | F02 |
| 接口覆盖率 | **≥70%** | TD:1173／FR-TC-06 |
| 闸门 B/D 语义 | **未被修改** | 尊重既有权限边界 |
| `SENSITIVE_FLOOR_PCT` | 仍 **95.0**，未放宽 | PRD 硬线 |

```bash
uv run python tests/eval_gate.py                       # 预期闸门 E 红，如实报差值
uv run pytest --cov=app --cov-report=term-missing -q    # 接口 ≥70%
bash .githooks/pre-push                                 # 无 python 时非 0
```

**红线**：**不许**为变绿调阈值、删用例、放宽 `SENSITIVE_FLOOR_PCT`；**不改 `rules.py`**（归 B）；失败归因回 B（路由）/C（分级）。

**预期结果说明**：本项**会让当前基线立刻变红**——这是门禁如实履职，不是你的失败。达标依赖 B3/B4 的路由改进，D11 16:00 复跑。

**CP-D3**：报告闸门 E 对当前基线的真实输出＋七层归因表＋覆盖率数字＋Q2 确认结果。

---

## 4. 节点 D4 · family 第五类 ＋ 语音条播放 ＋ 声纹定稿（2h：骨架 1h＋联调 1h）

**前置**：D3。**软依赖**：C3（通知/日报就绪）、C4（`audio_url`）。
**对接**：消费 C4 的 `audio_url`、B4 的 `late_taken_reported`（X8）。

### 4.1 现状实测

- `frontend/src/family/data-source.ts:16-21 MedicationStats` **四键**（`taken_reported`／`refused`／`missed_unconfirmed`／`notification_failed`），注释明写「键名与用药回执终态逐字对齐，**禁止合并为「实际漏服」**」（契约 S-01）。
- `frontend/src/family/pages/ReportsPage.tsx:15`「P0 恒 null，不渲染播放器」；`app/api/reports.py:74/:149 audio_url` 只读。
- `frontend/src/family/index.css` 正文均 **16px** ✓，但 **8 处 14px**：`.helper-error:134`、`.app-footer-note:322`、`.badge:334`、`.section-note:385`、**`.report-preview:441`**、`.detail-note:503`、`.event-time:533`、`.notif-meta:585`。多为辅助标签，**`.report-preview` 是日报预览正文，疑违 PRD line 95「正文 ≥16px」**。
- `.env:148 VOICEPRINT_SIMILARITY_THRESHOLD=**0.55**`；`.env.example:155` 与 `app/core/config.py:227` 均 **0.62**（`config.py:223-226` 注释记录 PRD v3.1 裁决沿革）；`voiceprint_service.py:323` 读 settings → **运行时生效 0.55**。`.env` **未被 git 跟踪**（`.gitignore:13-14`），**不构成密钥泄露**。
- `eval/reports/声纹调优报告.md` **已产出**：§四 FAR/FRR 扫描表（主区间 0.45–0.70）＋`voiceprint_sweep_raw.json` 可复跑；PRD v3.1 已据此裁 **0.62**（FAR 3.7%／FRR 0）。**报告正文仍以 0.55 为基线叙述，需补 0.62 定稿节**。
- `eval/reports/day07_q2_eval.md` 即 **Laya 单基线**（运行时唯一分类器，类名已由 `ChineseJevBackend` 更名 `LayaBackend`）。**Laya/Chinese-Jev 两 backend 对比已裁减**——不存在第二个 backend，对比无对象（TD:1173 该条失去意义，见总览 §8.1）。该文件保留作路由基线，**不需新建对比报告**。

### 4.2 要做什么

1. **F10 family 第五类统计**：`MedicationStats` 增 **`late_taken_reported`** 键（与终态逐字对齐）；**走契约 §7 留痕**（契约 S-01 原为四键）；**禁止合并为「实际漏服」**。
2. **F30 语音条播放**：`ReportsPage.tsx` 渲染播放器（消费 C4 的 `audio_url`）；null 时不渲染（保持既有降级）。
3. **375px 字号复核**：`.report-preview` 改 **≥16px**；其余 7 处辅助标签逐一判定是否属「正文」，属正文的一并改；产出 **375px 无横向滚动截图**。
4. **F28 `.env` 阈值**：改本机 `.env` 为 **0.62**（**环境项，不入库**）；启动自检确认生效值。
5. **声纹报告定稿**：补 **0.62 定稿节**（FAR 3.7%／FRR 0、样本量 3 人 9 段、模型 CAM++ fp=f682b514、阈值结论），保留 0.55 历史数据作反证。
6. ~~Laya/Jev 对比报告~~ **已裁减，不做**（无第二 backend；见总览 §8.1）。

### 4.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `frontend/src/family/` 五键统计＋语音条播放器＋字号修正 |
| 2 | 契约 S-01 第五键变更留痕 |
| 3 | 375px 走查截图（无横向滚动） |
| 4 | `.env` 阈值 0.62（不入库）＋启动自检输出 |
| 5 | `eval/reports/声纹调优报告.md` 0.62 定稿节 |

### 4.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| family 统计键 | **五键**与终态逐字对齐（含 `late_taken_reported`）；**未合并为「实际漏服」** | 契约 S-01／PRD:320-325 |
| 日报语音条 | `audio_url` 非空时**可播**；null 时不渲染播放器 | FR-RPT-04 |
| 375px | **无横向滚动**（截图为证）；`.report-preview` **≥16px** | PRD:95 |
| 生效声纹阈值 | 启动自检显示 **0.62** | PRD v3.1／AGENTS §7 |
| 声纹报告 | 含 **0.62 定稿节**：FAR **3.7%**／FRR **0**、样本量、模型版本、阈值结论 | FR-VPR-04／TD:1173 |
| 三档身份级联 | 演示脚本开场自检（阈值 0.62 生效确认）通过 | day08 演示脚本先例 |

```bash
pnpm --dir frontend exec tsc --noEmit && pnpm --dir frontend build
uv run python scripts/eval_router.py --fixtures eval/fixtures/day7_intents.jsonl --provider local
# 375px 视口截图；启动日志核对阈值
```

**CP-D4**：报告五键对照＋375px 截图＋阈值自检输出＋两份报告路径与关键数字。

---

## 5. 节点 D5 · 合规戏曲素材种子（2h）

**前置**：N0（X10 规范）。**无其他硬前置，可穿插在 D3 等待期。**
**对接**：**提供 X10**（合规素材）给 C4 的 `seed_media_library`。

### 5.1 现状实测

- `scripts/media_manifest.json` 内容为 **流行/摇滚/说唱/民谣**：cruel summer（流行）／the cure（流行）／仙儿（摇滚）／冷战（说唱）／她说（流行）／安河桥（民谣）／心墙／我要开花（摇滚）／狂恋（说唱）／玫瑰花的葬礼／茉莉雨／阿楚姑娘／除了快乐禁止入内／雅俗共赏／霸王别姬 等。
- `scripts/seed_media_mp3.py` docstring：真下载 mp3、duration 从帧头取、genre/era 只从 manifest 取，**所有行 `type=opera`**；`static/media/opera/` 实存 **15 个 UUID 命名 mp3**；`git ls-files static/media` **0**（`.gitignore:78`）。
- **`scripts/generate_opera_seed.py:1-45` 是合规的**：程序生成 ~3s wav、20–30 条、**越剧/京剧/黄梅戏/豫剧/评剧**、1950s–1990s（如「梁山伯与祝英台·楼台会／袁雪芬／42s」「红楼梦·宝玉哭灵」「追鱼·拔鳞」「碧玉簪·送凤冠」「西厢记·琴心」）；`:16` 注明应改调 `media_service.seed_media_library`——**但未被采用**。
- 契约《接口契约_照护台业务数据》**§6.4**（已冻结）：20–30 条／title 中文剧名／**genre ≥4 剧种且必含越剧京剧**／duration **15–60 秒**。

### 5.2 要做什么

1. **改用 `generate_opera_seed.py`**，并按其 `:16` 注释**改调 `media_service.seed_media_library`**（C 持锁，你只调函数不直写 ORM）。
2. **替换 `scripts/media_manifest.json`** 为合规剧种/剧名/年代数据；`seed_media_mp3.py` 的流行歌路径**停用或删除**（不保留两套种子实现，AGENTS §4.8）。
3. **重建 `static/media/opera/`**；duration 落 **15–60s**（程序生成音频若不足 15s 须补足或调整元数据口径，**不得虚报时长**）。
4. **验证重建流程**：在新克隆机器（或清空目录后）跑通脚本。

### 5.3 交付物清单

| # | 交付物 |
| --- | --- |
| 1 | `scripts/generate_opera_seed.py` 改调 `seed_media_library` |
| 2 | `scripts/media_manifest.json` 合规替换 |
| 3 | `static/media/opera/` 重建（不入库） |
| 4 | 重建流程验证记录（清空后跑通） |
| 5 | AC-37 点播演示证据（与 C4/B3） |

### 5.4 量化验收

| 判据 | 期望 | 依据 |
| --- | --- | --- |
| 种子条数 | **20–30 条** | 契约 §6.4 |
| 剧种 | **≥4 剧种且必含越剧、京剧** | 契约 §6.4 |
| `title` | **中文剧名** | 契约 §6.4 |
| `duration` | **15–60 秒**，与实际音频一致（不虚报） | 契约 §6.4 |
| `grep -E "流行\|摇滚\|说唱\|民谣" scripts/media_manifest.json` | **零命中** | F21 |
| 写库口 | 只调 `media_service.seed_media_library`，**脚本内无直写 ORM** | AGENTS §2.1 |
| 重建 | 清空 `static/media/opera/` 后脚本**跑通** | AGENTS §4.11 配套 |
| AC-37 | 点播越剧/京剧可播、可控制 | AC-37 |

```bash
uv run python scripts/generate_opera_seed.py
# SQL 核对：SELECT count(*), count(DISTINCT genre) FROM media_library WHERE type='opera';
#   期望 count 20–30、distinct genre ≥4 且含越剧/京剧
```

**CP-D5**：报告条数/剧种/时长分布 SQL 输出＋重建验证记录＋AC-37 演示证据。

---

## 6. 执行记录与对接登记（待回填）

| 日期 | 节点 | 提交 | 验收结果 | 等待项 |
| --- | --- | --- | --- | --- |
| — | D1 | — | — | — |
| — | D2 | — | — | — |
| — | D3 | — | — | — |
| — | D4 | — | — | — |
| — | D5 | — | — | — |

**对接登记**：

| 对接点 | 对方 | 我提供/消费 | 冻结时点 | 联调时点 | 状态 |
| --- | --- | --- | --- | --- | --- |
| X1 回执帧 | C3 | **提供** | N0 | D10 12:00 | 待 |
| X8 `rtc_failed` 枚举 | A1 | 消费 | N0（内容已定） | 提前交付·待工作区确认 | 协调人 2026-11-12 17:17 宣布已交付验收；工作区 alembic 仍仅 0019，写库前须 `alembic upgrade head` 确认 0020 已应用 |
| X9 门禁硬线与归因 | 全员 | **提供** | N0 | D11 16:00 | 待 |
| X10 素材规范 | C4 | **提供** | N0 | D11 10:00 | 待 |
| 编排入口稳定 | B3 | 消费 | — | D11 上午 | 待 |
| 路由改进完成 | B3/B4 | 消费（复跑门禁） | — | D11 16:00 | 待 |
| `audio_url` | C4 | 消费 | — | D11 下午 | 待 |
| `late_taken_reported` | B4 | 消费 | — | D11 下午 | 待 |
| boundary 判黄改动 | B3 | 消费（夹具对齐） | — | D11 16:00 | 待 |

**高风险文件方案登记**（`app/audio/**`／`app/api/audio.py`，落代码前必填）：待填。

---

## 7. 风险与回退

| # | 风险 | 处置 |
| --- | --- | --- |
| R-D1 | **D2 是本包最重节点（7h）且含两项不得裁减** | 拆三段并行推进：先 ICE＋外呼骨架（不依赖 B3），降级进路由待 B3 入口稳定后联调；**F16/F17 无论多紧都不顺延**（PRD:590） |
| R-D2 | 闸门 E 上线即红，阻塞全员提交 | 如实报红是预期结果。若需过渡期，**须主理人显式裁决「E 报告但不阻塞」并留痕**（总览 Q2），**不许默认放行** |
| R-D3 | 反向外呼需 pad 自动接听，浏览器自动播放策略可能拦截 | 沿用既有用户手势解锁音频上下文的口径；自动接听失败 → 降级页面播报＋`rtc_failed`（PRD:575 口径），**不静默失败** |
| R-D4 | KWS 资产改布局后新克隆机器无法重建 | 同步更新 `scripts/build-kws-wasm.md`；资产不入库属合规，**重建流程必须可跑**（D5 同口径） |
| R-D5 | 夹具 gold 改判黄后敏感集召回回退 | 与 B3 的 `safety.py` 改动**同场联调**；≥19/20 是硬线，回退即撤销该次改动 |
| R-D6 | 素材时长不足 15s（程序生成 ~3s wav） | 补足音频长度或按契约 §6.4 调整生成参数；**不得虚报 duration 元数据** |
| R-D7 | 日历口径压缩（总览 Q1） | 先裁顺位：**D5 素材（若 C4 未就绪）→ F29 ICE（coturn 本体本就归 Day 10）**；Laya/Jev 对比报告已裁减不占顺位；**D1 的 F16 唤醒词、D2 的 F15/F17、D3 的 F02 门禁属 P0/不得裁减，不容顺延** |

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.3 | 2026-11-12 | **Laya/Chinese-Jev 对比报告裁减**（主理人决定：运行时唯一分类器即 Laya，无第二 backend，对比无对象）：D4 删除对比报告交付物与验收、3h→**2h**，D 包 20h→**19h**；`day07_q2_eval.md` 保留作 Laya 单基线。配套代码侧已将 `ChineseJevBackend` 更名 `LayaBackend`（功能不变，ruff＋66 测试通过）；conda 环境名 `jev` 与 `spikes/chinese_jev_hello` 为机器事实/历史留痕，保留。 |
| v1.2 | 2026-11-12 | D1 验收补**退房被动链路联调判据**（M5.4，配合 A3/C6）：pad 旧 token→401 回登录/未授权态、原绑定家属→403 空态。**D 负责前端表现断言，不需新前端代码、不改后端**（两端兜底已存在：`pad/api.ts:58-63`、`family/api.ts:265`、`HomePage.tsx:91`）。 |
| v1.1 | 2026-11-12 | 全量校验后修正：① **F29 ICE 配置下发端点落点明确为 `app/api/audio.py`**（你持锁），消除「服务端取」的模糊表述；② X1 联调时点随 C 队列调整为 **D10 12:00**（C3 骨架 10.5h 完成，D1 骨架 4.5h 完成，双方均就绪）；③ 排程校验后队列确定为 **D1→D5→D2→D3→D4**（D5 素材提前到 D9 下午，确保 C4 在 D11 10:00 X10 联调时可用）。程序校验：硬前置 11 边、软依赖 18 边**零违例**，五里程碑全达标；包容量 20h＝2.5 人日不变。 |
| v1.0 | 2026-11-12 | 首版。承接 day08 遗留 13 项（F01 pad 回执/F02/F03 pad SOS/F10 family 第五类/F15/F16/F17/F21 素材/F22 夹具 gold/F24/F28/F29/F30 播放/F31）＋ Day 9 原生声纹报告定稿与 Laya/Jev 对比报告、接口覆盖率 ≥70%；5 节点 20h＝2.5 人日。**明确两项不得裁减**（F16 唤醒词、F17 反向外呼，依据 PRD:590），**否决 day08 审查计划 Q1 的顺延建议**。实测校准：`TaskPanel.tsx:31-37` 确实不传 `onRendered`、撤销确为纯 `setState`、SOS 三格确为硬编码、KWS 资产确多一层目录、声纹 FAR/FRR 报告**已产出**（只补 0.62 定稿节）、`day07_q2_eval.md` 是单 backend 基线**非对比报告**、ASR 置信度**已埋点**（七层归因须接真实数据而非写「无数据」）。 |