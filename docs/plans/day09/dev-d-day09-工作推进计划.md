> 状态：草稿（待主理人确认执行）　｜　版本：v1.1　｜　日期：2026-11-12
> 负责人：**dev-D（唯一）**　｜　任务包：D · 长者端语音补全 ＋ 评测门禁 ＋ 家属端　｜　容量：**17h ＝ 2.125 人日**（骨架 13h ＋ 联调 4h；对比报告与 D5 戏曲素材裁减后）
> 关联：docs/team_tasks/day09/day09-任务总览与DAG.md v1.5（§3.5 队列、§4.4 分工、§5 对接点）｜docs/team_tasks/day09/dev-D-长者端语音与评测门禁.md v1.3（任务书，本文的事实源）｜docs/team_tasks/day09/contracts.md **v0.4**（X1/X8/X9 消费与提供；~~X10~~ 已裁减作废并同步进契约文件）
> 上游依据：PRD line 95/247/257/286/320-325/336/387/401-412/531-536/547/586-590｜技术设计文档 line 778/1166-1182
> 遗留承接：F01/F02/F03/F10/F15/F16/F17/F22/F24/F28/F29/F30/F31 共 13 项（~~F21 素材~~ 已随 D5 裁减；F21 的 C4 service 接线与 B3 rules 剧种词部分归对方自行处置）
> 变更记录见文末

---

# dev-D · Day 9 工作推进计划（长者端语音与评测门禁）

## 0. 目标与执行边界（先读这条）

### 0.1 包目标

一天半内完成五件事，全部有量化验收：

1. **D1** 通知回执真链路（X1 帧）＋ pad 撤销调后端 ＋ SOS 真实态 ＋ KWS 目录修复（4h）
2. **D2** 降级上传进路由＋120s ＋ 反向 WebRTC 外呼（**不得裁减**）＋ ICE 配置（7h）
3. **D3** 评测门禁闸门 E（PRD 硬线）＋ 七层归因真实数据 ＋ 夹具判黄 ＋ pre-push 真门禁（4h）
4. **D4** family 第五类统计 ＋ 日报语音条播放 ＋ 375px 字号复核 ＋ 声纹 0.62 定稿（2h）
5. ~~**D5** 合规戏曲素材种子~~ **已裁减（v1.1，团队一致决定，留痕见 §2.2）**

### 0.2 写域（越界即返工）

`app/api/audio.py`、`app/api/reports.py`、`app/api/family.py`（增量）、`app/audio/**`、`frontend/src/pad/**`、`frontend/src/family/**`、`frontend/public/**`、`tests/eval_gate.py`、`eval/fixtures/day7_intents.jsonl`、`eval/reports/**`、`.githooks/**`、`scripts/**`、`static/media/**`（不入库）、对应 tests。

### 0.3 禁触（他人写锁）

`app/models/**`、`alembic/**`（A）、`app/services/**`（A/B/C）、`app/graphs/**`、`app/scheduler/**`、`app/agents/**`、`app/router/**`、`app/main.py`（B）、`app/core/**`（A/C）、`frontend/src/console/**`（C）、`eval/fixtures/injection.jsonl`／`tests/test_injection.py`（B4）、`tests/test_gates.py`（A2）。`app/api/__init__.py` 本日无需追加（新端点落已持锁的 `audio.py`/`reports.py`/`family.py`）。

### 0.4 高风险文件前置门槛（AGENTS §5）

`app/audio/**`（WebRTC 建连与媒体）与 `app/api/audio.py`（上传降级鉴权）属高风险文件：**D2 落代码前必须先在本文 §6.2 登记方案并经主理人确认**，人工逐行读 diff。

### 0.5 两项「不得裁减」（PRD:590）

**F16 唤醒词（D1）** 与 **F17 反向外呼（D2）**——无论容量多紧不顺延；day08 审查计划 Q1 的顺延建议已被总览否决。

---

## 1. 执行顺序与时间分配总表

队列（v1.1 随 D5 裁减调整）：**D1 → D2 → D3 → D4**。有软前置的节点拆「骨架段（连续排）／联调段（挂固定时点）」两段。

| 节点 | 内容 | 骨架 | 联调 | 合计 | 优先级 | 时段安排 |
| --- | --- | --- | --- | --- | --- | --- |
| N0 | 契约冻结评审（X1–X12 签认，B 牵头） | — | — | 0.5h | **P0·硬前置** | D9 09:00–09:30 |
| **D1** | 回执帧＋撤销＋SOS＋KWS | 4h | （含 X1 联调 D10 12:00，0.5h 计入证据留证） | 4.5h | **P0·不得裁减项在包** | D9 上午→午后 |
| **D2** | 降级进路由＋外呼＋ICE | 5h | 2h | 7h | **P0·F17 不得裁减** | D9 下午→D10 |
| **D3** | 门禁＋归因＋夹具＋hooks | 3h | 1h | 4h | **P0**（X9 提供方） | D10 午后→D11 上午 |
| **D4** | family 第五类＋语音条＋声纹定稿 | 1h | 1h | 2h | P1（软依赖 C3/C4） | D11 |
| ~~D5~~ | ~~合规戏曲素材~~ | — | — | ~~2h~~ | **已裁减**（v1.1） | — |
| — | 收口：执行记录回填、CP 证据归档 | — | — | 缓冲 | — | D11 下午 |

骨架合计 13h；联调段 4h（D2 2h／D3 1h／D4 1h）＋ N0 0.5h，总计 17h（裁减释放 2h 作缓冲与全员联调支援）。**关键路径 `N0 → D1 → D2`（骨架 9.5h）**，D1 是 D 包最前端，**D9 上午第一个开工（C3 在等 X1）**。D5 裁减后 D2 骨架可提前到 D9 下午开始。

---

## 2. 任务拆解（最小可执行子任务）

### 2.1 节点 D1 · 回执帧 ＋ pad 撤销 ＋ SOS 真实态 ＋ KWS 目录（4h）

**前置**：N0（X1 帧形状冻结）。**对接**：提供 X1 给 C3；消费 A1 的 X8（`rtc_failed`，D2 用，本节点不阻塞）。

| # | 子任务 | 耗时 | 优先级 | 前置 | 验收标准（完成判定） |
| --- | --- | --- | --- | --- | --- |
| D1.1 | `TaskPanel.tsx:31` 补传 `onRendered`，回调内按 X1 发 `client_receipt` 帧（`receipt_kind`/`task_id`/`notification_id`/`ts`，不自造 task_id） | 0.5h | P0 | N0 | 渲染卡片后信令 WS 可见上行帧；帧字段与 contracts.md X1 逐字一致 |
| D1.2 | `app/audio/signaling.py` 收帧：校验 `task_id` 归属当前设备绑定长者 → 调 C 的 `mark_notification_delivered`；幂等键 `(notification_id, receipt_kind)`；`elder_id`/`device_id` 服务端注入、客户端传值丢弃 | 1h | P0 | D1.1 | 重复回执返回原结果＋`duplicate=true`，**不重复写 `sent_at`**；伪造归属 → 拒绝 |
| D1.3 | `MedicationCard.tsx:121` 撤销改调 `app/api/pad_undo.py` 既有端点，成功后才回滚 UI；失败显示可重试 | 0.5h | P1 | 无 | 点撤销 → DB 终态回退；后端拒绝时 UI **不误显示已撤销**（FR-UI-08） |
| D1.4 | `SosResult.tsx` 三格状态改后端真实通知状态驱动；文案分级；保留「非急救通道」声明与「我说错了」入口（AC-26：红色事件不可一键撤销） | 1h | P0 | D1.2 | 断开通知出口后按 SOS，页面**不得出现「已通知」**；「我说错了」可用、事件保持 open |
| D1.5 | KWS 资产移平铺 `frontend/public/kws/`（删 `__MACOSX/`）；加载失败不再静默——显式提示「唤醒不可用，请按住说话」＋降级标记；同步核对 `scripts/build-kws-wasm.md` | 1h | **P0·不得裁减** | 无 | 三资产（js/.wasm/.data）Network 全 200；说「小伴」唤醒 ≤1.5s、20 次 ≥18 达标；改错路径 → 显式降级提示 |
| D1.6 | X1 联调证据（与 C3，D10 12:00）＋ 唤醒 20 次延迟表 ＋ 退房被动链路前端断言（M5.4，配合 A3/C6：pad 旧 token→401 回登录、原绑定家属→403 空态，**只验证不新写代码**） | 0.5h | P0 | D1.2 | pad 渲染卡片 → DB 行 `status='delivered'`；不渲染 → 保持 `pending`（三次扫描后 failed） |

**注意**：`frontend/public/kws/` 被 `.gitignore:89` 排除属合规（不提交大文件），不改 gitignore；重建流程须新克隆机器可跑。

**CP-D1 检查点**：9 条判据实测 ＋ 三资产状态码 ＋ 唤醒延迟表。**D10 12:00 前与 C3 完成 X1 联调。**

### 2.2 ~~节点 D5 · 合规戏曲素材种子~~ —— **已裁减（v1.1，2026-11-12 团队一致决定）**

> **裁减留痕（PRD §7「裁减有记录」口径）**：
> - **决定**：不加戏曲素材，媒体库以现有歌单为准（「已经有歌了」）。D5 整节点（2h）移出 D 包，容量 19h→**17h**。
> - **依据**：PRD §6.2 裁减顺序第 2 条明列「P2 可选项：……**戏曲素材**」——素材级裁减有 PRD 依据；点播功能本体（FR-CON-04，Day 7 已交付）不受影响。
> - **关联方通知义务（D5 与其他组员的关系，裁减须同步）**：
>   - **dev-C（C4）**：D5 原是 **X10 契约提供方**（素材 → C4 的 `seed_media_library` 消费，联调 D11 10:00）。X10 按 X6 先例标注**已裁减作废，编号保留不复用**；C4 的 media 接线改以现有媒体库为对象。
>   - **dev-B（B3）**：F21 三方拆分中的 rules 剧种词部分——无戏曲素材可命中后，B3 的 `rules.py` 剧种词规则改法需 B 自行调整，D 不再是素材依赖方。
>   - **契约层**：《接口契约_照护台业务数据》§6.4（20–30 条／≥4 剧种含越剧京剧／15–60s）随之**不再适用**——现状 `scripts/media_manifest.json`（流行歌 `type=opera`）本就违反 §6.4，裁减后以留痕明确「以现有歌单为准」。
>   - **验收层**：**AC-37**（点播越剧/京剧可播）改为「**点播现有媒体库条目可播、可控制**」口径——此口径变更属验收基线调整，**须在 day09 总览（§8 裁减节）与 PRD 层留痕**，不由 D 单方面改判。
> - **原 D5 交付物全部作废**：`generate_opera_seed.py` 改调、manifest 替换、`static/media/opera/` 重建、重建验证、AC-37 戏曲演示证据均不再执行；相关脚本与目录**保留现状不删**（历史留痕，不保留两套实现的处理也随裁减一并搁置）。

### 2.3 节点 D2 · 降级进路由 ＋ 反向外呼 ＋ ICE 配置（7h＝骨架 5h＋联调 2h）

**前置**：D1（复用同一信令与回执通道）。**软依赖**：B3 编排入口稳定（**可先做外呼与 ICE，不等待**）；消费 X8（A1 迁移 0020，D9 12:00 交付）。

| # | 子任务 | 耗时 | 优先级 | 前置 | 验收标准 |
| --- | --- | --- | --- | --- | --- |
| D2.1 | **方案先行**：`app/audio/outbound.py`（服务端主动外呼）与 `app/api/audio.py` 改动方案登记 §6.2，经主理人确认后再落代码 | 0.5h | **P0·门槛** | D1 | §6.2 已填、确认留痕 |
| D2.2 | F15 降级进路由：删除 `audio.py:311-312` 写死回复，ASR 文本走与 WS FINAL **同一条编排入口**（`user_text` 同路，参照 `MedicationCard.tsx:75` 口径）；上限 30s→120s；删除以大纲为依据的注释、改注 PRD:247 | 1.5h | **P0** | D2.1＋B3 入口 | 上传「今天天气怎么样」→ 真实天气工具结果；急症语料 → 安全一票否决不进大模型（AC-04）；121s → **422**；`grep "大纲" app/api/audio.py` 零命中 |
| D2.3 | F17 反向外呼：新建 `app/audio/outbound.py`，复用既有信令 WS（**不新建信令服务**）；呼叫失败落 `failure_reason='rtc_failed'`（依赖 X8） | 2h | **P0·不得裁减** | D2.1＋X8 | 服务端发起外呼 → pad 自动接听 → 双向音频通（FR-MED-06/AC-41）；失败 `rtc_failed` 落库；外呼失败降级页面播报（PRD:575 口径） |
| D2.4 | `frontend/src/pad/task-channel.ts` 落地自动接听主路径（删除「尚无自动接听能力」自述） | 1h | P0·不得裁减 | D2.3 | pad 无人工干预自动接听建连并播报 |
| D2.5 | F29 ICE：`app/api/audio.py` 增 ICE 配置下发端点；`realtime.ts:365` 从端点取 iceServers（含 TURN）；TURN 凭据环境注入不硬编码；coturn 本体归 Day 10 | 1h | P2 | D2.1 | `RTCPeerConnection` 含 iceServers；TURN 凭据不出现在前端源码与日志（AGENTS §4.2）；信令仍 `/ws/signal` |
| D2.6 | 联调段：与 B3 对齐降级编排入口 ＋ AC-41 端到端证据 ＋ 降级三用例证据 ＋ `rtc_failed` 落库行 | 2h | P0 | D2.2–D2.4 | `grep -rn "rtc_failed" app/ frontend/src/` 有命中（此前零命中）；pytest `-k "audio or outbound or webrtc"` 全绿 |

**CP-D2 检查点**：降级三用例真实应答 ＋ 121s 边界 ＋ 外呼双向音频证据 ＋ `rtc_failed` 落库行。**D2 完成＝M5.3 判据达成（D11 13:00）。**

### 2.4 节点 D3 · 门禁硬线 ＋ 七层归因 ＋ 夹具判黄 ＋ hooks（4h＝骨架 3h＋联调 1h）

**前置**：软依赖 B3/B4 路由改进（**本体可先建，对当前基线如实报红**）。**对接**：**提供 X9 给全员**。

| # | 子任务 | 耗时 | 优先级 | 前置 | 验收标准 |
| --- | --- | --- | --- | --- | --- |
| D3.1 | F02 闸门 E：`tests/eval_gate.py` 新增闸门 E（普通集 ≥90%＝45/50、p95 ≤200ms、注入拦截 100%），与既有闸门 B/D 并列独立、不改其语义；失败打印未达标项与差值、退出码非 0；写锁文件须显式 `--allow-below-prd`＋留痕 | 1.5h | **P0** | 无 | 当前锁文件（82%/297ms）跑 E **必须失败**并打印两项差值；构造 45/50＋195ms 假锁 → E 通过；`SENSITIVE_FLOOR_PCT` 仍 95.0；闸门 B/D 未被修改 |
| D3.2 | F31 七层归因接真实数据：ASR/分类器/规则层/召回/LLM/Tool/编排七层均真实值或带原因的空值，删除硬编码「无数据」；ASR 层接既有埋点（`_observe.py:69` 等）；funasr confidence 恒 None 如实标注数据源限制 | 1h | P1 | 无 | 归因表无硬编码「无数据」；空值带原因 |
| D3.3 | F22 夹具 gold 修正：`eval/fixtures/day7_intents.jsonl` boundary 20 条由 `other`/`is_sensitive=False` 改判黄复核口径（PRD:272）；属向 PRD 对齐非改验收 | 0.5h | P1 | 无（与 B3 的 safety 改动同场联调） | 改判黄后敏感集 ≥19/20 **不回退**；回退即撤销该次改动 |
| D3.4 | F02 hooks：`.githooks/pre-push` 无 python 时**报错退出**而非 `exit 0`；真门禁接线（阻塞模式需主理人确认＝总览 Q2，默认按 AGENTS §3 阻塞）；提示启用 `git config core.hooksPath .githooks` | 0.5h | P1 | D3.1 | 无 python 环境跑 pre-push 退出码非 0 |
| D3.5 | 接口覆盖率 ≥70% 报告（TD:1173）＋ X9 冻结口径对全员通报 ＋ 联调复跑（D11 16:00，待 B3/B4 路由改进后） | 1h | P0 | D3.1–D3.4 | `pytest --cov=app --cov-report=term-missing` ≥70%；覆盖率报告文件落 `eval/reports/` |

**红线**：不许为变绿调阈值、删用例、放宽 `SENSITIVE_FLOOR_PCT`；不改 `rules.py`（归 B）；失败归因回 B（路由）/C（分级）。**预期本项会让当前基线立刻变红——这是门禁如实履职，不是失败；达标依赖 B3/B4，D11 16:00 复跑。**

**CP-D3 检查点**：闸门 E 真实输出 ＋ 七层归因表 ＋ 覆盖率数字 ＋ Q2 确认结果。

### 2.5 节点 D4 · family 第五类 ＋ 语音条播放 ＋ 声纹定稿（2h＝骨架 1h＋联调 1h）

**前置**：D3。**软依赖**：C3（通知/日报就绪）、C4（`audio_url`）、B4（`late_taken_reported`，X8）。

| # | 子任务 | 耗时 | 优先级 | 前置 | 验收标准 |
| --- | --- | --- | --- | --- | --- |
| D4.1 | F10：`frontend/src/family/data-source.ts` `MedicationStats` 增 `late_taken_reported` 第五键（与终态逐字对齐）；**走契约 §7 留痕**（S-01 原四键）；**禁止合并为「实际漏服」** | 0.5h | P1 | D3 | family 统计五键与终态逐字对齐 |
| D4.2 | F30：`ReportsPage.tsx` 渲染语音条播放器（消费 C4 的 `audio_url`）；null 时不渲染（保持既有降级） | 0.25h | P1 | C4 | `audio_url` 非空可播、null 不渲染播放器（FR-RPT-04） |
| D4.3 | 375px 字号复核：`.report-preview` 改 ≥16px；其余 7 处 14px 逐一判定是否属正文；产出 375px 无横向滚动截图 | 0.25h | P1 | 无 | 375px 无横向滚动（截图为证）；正文 ≥16px（PRD:95） |
| D4.4 | F28：本机 `.env` 阈值改 0.62（**环境项不入库**）；启动自检确认生效值 | 0h（环境项） | P2 | 无 | 启动自检显示 0.62；三档身份级联演示脚本开场自检通过 |
| D4.5 | 声纹报告定稿：`eval/reports/声纹调优报告.md` 补 0.62 定稿节（FAR 3.7%/FRR 0、样本量 3 人 9 段、模型 CAM++ fp=f682b514、阈值结论），保留 0.55 历史数据作反证。~~Laya/Jev 对比报告~~已裁减不做 | 0.5h | P1 | D4.4 | 定稿节含 FAR/FRR/样本量/模型版本/阈值结论（FR-VPR-04） |
| D4.6 | 联调段：与 C3/C4 对齐日报与五键展示（D11 14:00）＋ CP 证据归档 | 1h | P1 | D4.1–D4.5 | 五键对照 ＋ 375px 截图 ＋ 阈值自检输出 ＋ 两份报告路径与关键数字 |

**CP-D4 检查点**：五键对照 ＋ 375px 截图 ＋ 阈值自检输出 ＋ 报告关键数字。

---

## 3. 依赖说明（前置条件与依赖资源）

### 3.1 包内依赖链

```
N0（契约冻结 09:30，硬前置）
 └─→ D1 ──→ D2 ──→ D3 ──→ D4
            │
            └─（D2.2 降级进路由需 B3 编排入口稳定，先做 D2.3–D2.5 不等待）
（D3 对当前基线如实报红，不等 B3/B4；D11 16:00 复跑）
（~~D5~~ 已裁减：X10 作废，C4/B3 通知义务见 §2.2 留痕）
```

### 3.2 跨成员对接点（我提供 ★ ／ 我消费 ◇）

| 对接点 | 对方 | 方向 | 冻结时点 | 联调时点 | 我的动作 |
| --- | --- | --- | --- | --- | --- |
| **X1 回执帧** | C3 | ★ 提供 | N0 | **D10 12:00** | 帧生产者＋`signaling.py` 收帧；调 C 冻结签名 `mark_notification_delivered` |
| **X8 迁移 0020**（`rtc_failed` 枚举） | A1 | ◇ 消费 | D9 12:00 交付 | 骨架后联调 | D2.3 消费；A1 未到前用桩约束开发 |
| **X9 门禁硬线与归因** | 全员 | ★ 提供 | N0 | **D11 16:00** | 闸门 E 判据与归因分工通报；只读阈值不改 `rules.py` |
| ~~**X10 素材规范**~~ | ~~C4~~ | ~~★ 提供~~ | **已裁减作废（v1.1，编号保留不复用，X6 先例）** | ~~D11 10:00~~ | 不再产素材；C4 的 media 接线改以现有媒体库为对象；AC-37 口径调整须在总览/PRD 层留痕 |
| 编排入口稳定 | B3 | ◇ 消费 | — | D11 上午 | D2.2 降级路径**不得另造第二条编排入口** |
| 路由改进完成 | B3/B4 | ◇ 消费（复跑门禁） | — | D11 16:00 | D3 复跑闸门 E，验收达标 |
| `audio_url` | C4 | ◇ 消费 | — | D11 下午 | D4.2 播放器只按冻结字段渲染 |
| `late_taken_reported` | B4 | ◇ 消费 | — | D11 下午 | D4.1 第五键；第五键变更走《家属端数据》契约 §7 留痕 |
| boundary 判黄改动 | B3 | ◇ 消费（夹具对齐） | — | D11 16:00 | D3.3 与 B3 的 `safety.py` 改动**同场联调** |

### 3.3 环境与资源前置（2026-11-12 17:08 实测更新）

- ~~N0 前通读 contracts.md X1/X8/X9/X10 四份口径并签认~~：**已通读**（含 v0.4，X10 已裁减）；**签认待 N0 团队评审**（X1/X8/X9 三份口径）。
- `frontend/public/kws/` 三资产就位：**✅ 已完成**（资产平铺＋删 `__MACOSX/`；vite preview 实测四资产全 **200**：js 9,925B／main.js 81,768B／wasm 12,758,227B／data 4,735,109B）。
- ~~`static/media/opera/` 本机可重建~~：**随 D5 裁减作废（v1.1）**。
- 后端本地起服＋compose：**✅ 已完成**（postgres/redis/voiceprint/两 MCP 容器 healthy；uvicorn 127.0.0.1:8000 已起，`/health` ok；vite preview 4173 伺服构建产物供资产核验）。
- `.githooks` 生效：**✅ 已执行** `git config core.hooksPath .githooks`（本机仓库配置；pre-push 真门禁接线在 D3.4 完成后即全链生效）。

---

## 4. 风险预案

| # | 风险 | 概率 | 处置预案 |
| --- | --- | --- | --- |
| R-D1 | **D2 是最重节点（7h）且含不得裁减项** | 高 | 拆三段并行：先 ICE＋外呼骨架（不依赖 B3），降级进路由待 B3 入口稳定后联调；F16/F17 无论多紧不顺延 |
| R-D2 | 闸门 E 上线即红（当前基线 82%<90%、297ms>200ms），可能被视为阻塞全员提交 | 高 | 如实报红是预期结果；若需过渡期，**须主理人显式裁决「E 报告但不阻塞」并留痕**（总览 Q2），不许默认放行 |
| R-D3 | 反向外呼的 pad 自动接听被浏览器自动播放策略拦截 | 中 | 沿用既有用户手势解锁音频上下文口径；自动接听失败 → 降级页面播报＋`rtc_failed`（PRD:575），**不静默失败** |
| R-D4 | KWS 资产改平铺布局后新克隆机器无法重建 | 中 | 同步更新 `scripts/build-kws-wasm.md`；资产不入库属合规，重建流程必须可跑（与 D5 同口径验证） |
| R-D5 | 夹具 gold 改判黄后敏感集召回回退（<19/20） | 中 | 与 B3 的 `safety.py` 改动同场联调；≥19/20 是硬线，回退即撤销该次改动并归因上报 |
| ~~R-D6~~ | ~~程序生成戏曲素材时长不足 15s（现状 ~3s wav）~~ | — | **随 D5 裁减作废（v1.1）** |
| R-D7 | 日历口径压缩（Day 9 实为 D9–D11 三工作日，总览 Q1） | 中 | 裁顺位：~~先裁 D5 素材~~（已裁减）→ F29 ICE（coturn 本体归 Day 10）；**F16 唤醒词、F15/F17、F02 门禁属 P0/不得裁减，不容顺延**；D5 释放的 2h 作缓冲 |
| R-D8 | X8（迁移 0020）D9 12:00 未按期交付，`rtc_failed` 无法落库 | 低 | 按契约骨架先行（service 逻辑＋桩约束），A1 交付后联调；不自行改 `app/models/**` |
| R-D9 | C3 的 `mark_notification_delivered` 签名 D10 12:00 未就绪 | 低 | 签名已随 X1 冻结，按冻结签名开发；联调延后不阻塞 D2/D3 骨架 |
| R-D10 | 高风险文件（`app/audio/**`、`app/api/audio.py`）diff 评审打回返工 | 中 | D2.1 方案先行门槛前置化解；落码后人工逐行读 diff，一次只提一个高风险 PR，缩小评审面 |

---

## 5. 里程碑与完成定义（D 包视角）

| 检查点 | 判据 | 时点 |
| --- | --- | --- |
| CP-D1 | X1 联调证据＋唤醒 20 次延迟表＋KWS 三资产 200 | D10 12:00 |
| CP-D2 | 降级三用例＋121s 边界＋外呼双向音频＋`rtc_failed` 落库（＝M5.3） | D11 13:00 |
| CP-D3 | 闸门 E 真实输出＋七层归因表＋覆盖率 ≥70% | D11 16:00（X9 联调） |
| CP-D4 | 五键对照＋375px 截图＋阈值 0.62 自检＋声纹定稿 | D11 18:00 前 |
| M5.3 | 唤醒 ≤1.5s（20 次 ≥18）／降级问天气真实应答／外呼自动接听双向通／失败落 `rtc_failed` | D11 13:00 |
| M5 | AC-08/20/23/24/26/41/42 相关项全过；~~AC-37 戏曲点播~~按「点播现有媒体库条目可播可控」口径（待总览/PRD 留痕）；FAR/FRR 按 0.62 定稿；门禁 E 如实判定；覆盖率 ≥70% | D11 18:00 |

---

## 6. 执行记录与登记（待回填）

### 6.1 节点执行记录

| 日期 | 节点 | 提交 | 验收结果 | 等待项 |
| --- | --- | --- | --- | --- |
| 2026-11-12 | D1（代码收口） | 工作区（待提交） | tsc/build 全绿；pytest 全量 **1401 passed**（新增 tests/test_client_receipt.py 9 条）；KWS 资产已平铺、`__MACOSX/` 已删 | X1 与 C3 联调（D10 12:00）；唤醒 20 次延迟实测；退房被动链路前端断言（M5.4）；C3 的 `task_command` 帧补 `notification_id` 与 `receipt_kind` 签名升级 |
| 2026-11-12 | D2（代码收口） | 工作区（待提交） | 方案 A1/B1 已批准并落码：audio.py 降级进编排入口＋120s＋ICE 端点（`grep 大纲` 零命中、121s→413）；`outbound.py` 状态机＋webrtc 外呼方法与 answer 分支＋协议双帧＋pad 自动接听/ICE 接入；pytest 全量 **1428 passed**（新增 test_outbound.py 6 条）、tsc/build 绿 | AC-41 端到端联调（与 B4 触达链接线，D11 10:00）；**联调前提风险**：pad 待机期无 WS 常驻则外呼不可达（no_connection→降级播报可达性成立但自动接听不成立），WS 常驻改造归 B4/Day 10 裁决；`rtc_failed` 落库由 B4 service 层接线 |
| — | D3 | — | — | — |
| — | D4 | — | — | — |
| — | ~~D5~~ | **已裁减（v1.1）** | C4/B3 通知待发出；X10 已同步 contracts v0.5（develop） | — |

### 6.2 高风险文件方案登记（落代码前必填，AGENTS §5）

> **确认结果：已批准（2026-11-12 17:25，主理人「按你的来」）**——按 A1（app.state 直调编排入口）＋B1（复用活跃会话＋会话中跳过外呼走降级播报）＋ICE 端点下发组合落码。

#### 方案 A · `app/api/audio.py`（D2.2 降级进路由 + D2.5 ICE 下发）

**A-1 降级进路由（F15）**：
- 现状：`audio.py:311-312` 写死 `reply_text = f"您说的是『{asr_text}』…"`（依据注释是大纲，违反 AGENTS 铁律）；`MAX_AUDIO_SECONDS = 30`（`:78`）。
- 改法：删除写死回复，改调 **WS FINAL 同一条编排入口**——`orchestrator.handle_asr_final(device_id, trace_id, turn_id, text, audio_bytes, audio_sink)`（main.py:592 既有口径）。orchestrator 已挂 `app.state.dialogue_orchestrator`（main.py:866），**端点经 `request.app.state` 取用，不改 `main.py` 一行**（B 独占）。`audio_sink` 降级场景传 `None`（回复文本直接进 REST 响应体，不经 WS 分段）。安全一票否决（AC-04）由编排入口内的 safety 层天然生效，不另写第二套判定。
- 120s：`MAX_AUDIO_SECONDS = 30 → 120`（PRD:247），注释依据改 PRD 行号；`grep "大纲" app/api/audio.py` 归零。
- 归属：`elder_id` 仍由 `current_device` 服务端解出（既有 `device_id` 依赖不变），编排入参用服务端注入值。

**A-2 ICE 配置下发（F29）**：
- 新端点 `GET /api/audio/ice-config`（`current_device` 鉴权）：返回 `{stun_url, turn_url, turn_username, turn_credential, ttl}`，值全部来自 `app/core/config.py` 既有字段（`stun_url`/`turn_url`/`turn_username`/`turn_credential`/`turn_credential_ttl_seconds`）——**凭据环境注入，不硬编码、不写日志**（响应体含凭据但不落日志）。coturn 服务本体归 Day 10 compose，本项只做下发与前端接入。
- 前端 `realtime.ts:365`：建连前 fetch ice-config（失败回落空 iceServers，本地 host candidate 仍可用，不阻塞主链路），`new RTCPeerConnection({ iceServers })`。

#### 方案 B · `app/audio/outbound.py`（新建，D2.3 反向外呼）

- **不新建信令服务**（AGENTS §1）：复用 `/ws/signal` 与 `deliver(device_id, frame)`。
- 帧协议新增两条（走契约变更流程，X1 同批）：S→C `offer`（外呼场景，服务端主动发）、C→S `answer`（pad 自动应答回传）。现状信令只有 C→S offer / S→C answer（pad 主动呼叫方向），反向需补对向帧。
- 服务端流程：`initiate_outbound_call(device_id, *, reason) -> OutboundCallResult`：
  1. 经 signaling 连接注册表找到该 device 活跃连接 → 取/建其 MediaSession（`MediaHub` webrtc.py:2036 既有 `_sessions` 注册表，`ensure_session`/`get_session` 复用）；
  2. `create_peer_connection()`（含既有 ice_servers、下行播放轨）→ `pc.createOffer()` → `setLocalDescription` → `deliver(offer)`；
  3. 等待 C→S answer（超时 `webrtc_ice_timeout_seconds`×2，默认 10s）→ `setRemoteDescription` → ICE 协商 → 建连成功后走既有触达播报链（tts_text＋audio 帧）；
  4. **失败分类**：超时无 answer / answer SDP 非法 / ICE 失败 → 返回失败原因，由调用方落 `failure_reason='rtc_failed'`（X8 枚举已在库）→ 降级页面播报（既有 WS tts_text 链，PRD:575）。
- **调用方归 B**：B4 用药触达链在发起时调本函数（骨架期 D 侧用单测假 deliver 自验，与 B 的接线在联调段完成）。
- pad 自动接听（D2.4，`task-channel.ts`/`realtime.ts`）：收到 S→C offer 不等用户手势直接应答（浏览器自动播放策略靠既有用户手势解锁的 AudioContext 兜底）；应答失败 → 显式上报 + 接受降级页面播报，**不静默**。

#### 不动项（红线）

不新建信令服务；不改 `app/main.py`（编排入口经 app.state 取用）；不直写 `medication_events`（`rtc_failed` 落库由调用方 service 层完成，D 不碰 `app/services/**`）；TURN 凭据不出现在前端源码与日志；`_offer_seen` 单次 offer 语义不动（外呼用同一会话的单次握手，重复外呼先关旧会话）。

#### 已落码待追认（D1 遗留）

`app/audio/signaling.py` X1 收帧：已按冻结形状落码（2026-11-12，用户指令先开工）——形状校验/幂等缓存在本层，归属校验经 `medication_service.get_event`（audio→services 单向，不 import ORM），delivered 落库调 `notify_service.mark_notification_delivered`（TypeError 降级过渡）；独立短会话 commit。测试 9 条，全量 1422 passed。**待主理人追认。**

#### 已落码（D2，方案 A1/B1 已批准 2026-11-12 17:25）

- `app/api/audio.py`：④ 编排入口 `app.state.dialogue_orchestrator.handle_asr_final`（与 WS FINAL 同路，`turn_id` 服务端 uuid 生成、`audio_sink=None`）；`MAX_AUDIO_SECONDS=120`（PRD:247）；`GET /api/audio/ice-config`（凭据环境注入、不落日志）。
- `app/audio/outbound.py`（新建）：`initiate_outbound_call` 状态机——in_session 跳过（B1）／no_connection／rtc_failed 三类归因；answer 等待槽经 `notify_outbound_answer` 由 webrtc 分支唤醒；关会话兜底 `_safe_close`。
- `app/audio/webrtc.py`：`MediaState.OUTBOUND_OFFER_CREATED`；`MediaSession.create_outbound_offer`/`handle_outbound_answer`（复用 `_offer_seen` 单次握手语义）；`handle_signal_message` 增 C→S `answer` 分支（惰性 import 回调 outbound 防环）。
- 前端：protocol 双帧（S→C offer／C→S answer）；realtime.ts `loadIceServers`（缓存＋失败回落＋diag）＋试会话/外呼双 PC 接 iceServers；自动接听 `handleOutboundOffer`（临时麦克风＋autoplay 拦截显式提示）；teardown 收口 outboundPc。
- 测试：`tests/test_outbound.py` 6 条；全量 **1428 passed**；tsc/build/ruff 全绿。

### 6.3 自检命令汇总

```bash
pnpm --dir frontend exec tsc --noEmit && pnpm --dir frontend build
uv run pytest tests/ -q -k "audio or outbound or webrtc"
uv run python tests/eval_gate.py                    # 闸门 E 预期红，如实报差值
uv run pytest --cov=app --cov-report=term-missing -q   # 接口 ≥70%
bash .githooks/pre-push                             # 无 python 时退出码非 0
uv run python scripts/eval_router.py --fixtures eval/fixtures/day7_intents.jsonl --provider local
grep -rn "rtc_failed" app/ frontend/src/            # 期望有命中
grep -n "大纲" app/api/audio.py                     # 期望零命中
```

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.1 | 2026-11-12 | **D5 戏曲素材节点裁减**（团队一致决定：不加戏曲，媒体库以现有歌单为准）：D5 整节点 2h 移出，容量 19h→**17h**；队列改 **D1→D2→D3→D4**（D2 骨架提前到 D9 下午）；X10 按 X6 先例**裁减作废、编号保留不复用**；遗留承接由 14 项减为 13 项（F21 素材部分移出，C4 service 接线与 B3 rules 剧种词归对方处置）。**关联方通知义务已登记 §2.2**：C4（X10 消费方）、B3（rules 剧种词）；**AC-37 口径调整为「点播现有媒体库条目可播可控」，须在 day09 总览 §8 与 PRD 层留痕（D 不单方面改判）**。 |
| v1.0 | 2026-11-12 | 首版。依据 day09 总览 v1.5 与 dev-D 任务书 v1.3 制定：5 节点 19h（骨架 15h＋联调 4h），队列 D1→D5→D2→D3→D4；含 4 组对接点（X1/X8/X9/X10）、10 条风险预案、CP-D1–D5 检查点与高风险文件方案先行登记。明确两项不得裁减（F16 唤醒词、F17 反向外呼）与闸门 E 预期报红的口径。 |
