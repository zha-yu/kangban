> 状态：未开始　｜　负责人：dev-D（全栈 · 日报通知 API、family/pad 与评测门禁）　｜　日期：2026-11-11（Day 8）　｜　版本：v1.2（v1.1＋D6 路由降级提示条转入；合并 develop fded21b）
> 依据：docs/plans/day08/day-08-健康采集与三端后台.md v1.2 ｜ 对接点与全局节奏见同目录《day08-任务总览与DAG.md》

# dev-D 个人任务书

## 任务清单

| # | 任务（对应计划） | 人时 | 前置 | 交付物 | 验收标准（量化） |
| --- | --- | --- | --- | --- | --- |
| D1 | H6：家属日报 + 通知域 REST 收口 | 2h | B1（冻结契约）；A1（DailyReport 模型）；C2（联调段） | `app/api/reports.py`、`app/api/family.py` 增量 + `tests/test_reports_api*.py` | ① 日报列表/详情接口（家属仅绑定长者，AC-13/18 后半）；② 通知已读/人工确认接口收口（staff 侧确认走 alerts 域）；③ 推送状态查询接口（成功/失败/重试/手动重发为管理员能力）；④ 家属 A 访问长者 B 日报 403+审计；已读幂等；ContractAPIRoute + ROUTERS 挂载 |
| D5 | H16：评测基线 v1 + eval_gate 门禁（fixtures 段可先行） | 3h | fixtures 段无前置；gate 本体等 B2 桩测试绿（X9，14:00） | `eval/fixtures/` 扩充（路由固定测试集 50 例固化 + 六类边界：否定/多意图/超时/打断/重复/越权）+ `tests/eval_gate.py` + 分层归因报告 + bad case 回流记录 | ① FR-TC-09：人为调低规则层阈值后跑门禁**被拦截**；② 评测报告含 ASR/分类器/规则层/召回/LLM/Tool/编排七层归因表且可复跑；③ bad case 自动回流 fixtures 有记录；④ 固定测试集跑分 ≥ Day 7 路由基线不回退 |
| D2 | H10：family 切真 + 375px 验收（P0 部分①③） | 2.5h | D1 | `family/data-source.ts` F0/F1/F2 切真 + 走查截图 | ① 按 `day05/d-家属端切换层说明.md` 流程，Mock 删除条件不满足的保留开关；② 375px 无横向滚动截图留证、正文 ≥16px、按钮够大、高对比度；③ 家属仅见绑定长者日报；④ 日报文本与接口一致、含固定声明 |
| D4 | H14⑤：pad 戏曲播放模块 | 1.5h | C4（播放源就绪）+B4（素材就绪），X8 联调 | pad `<audio>` 播放组件（直连 media_url，PRD §2.1 媒体分发口径） | ① 播放/暂停/打断演示留证；② 与播报流互斥、可打断；③ 只按 X8 冻结 schema/path 消费，不得私改字段 |
| D3 | H11：pad SOS 接线 + 撤销边界（P1，**R1③ 超载先裁项**） | 2h | A3（敏感事件+工单链路） | `SosButton` 后端接线 + 撤销接口边界 + `tests/test_sos*.py` | ① 点击即生成敏感事件+护工工单，不经语音路由（FR-ALT-06）；② SOS 按下 30 秒内护工看板出现红色置顶工单（与 C 联演留证）；③ 普通确认 10 秒可撤销+审计；红色事件撤销尝试仅追加「说错了」、事件保持 open（AC-26 口径）；④ 按钮 ≥48×48px |
| D6 | L6#10 转入：路由降级提示条（P2，**前置=主理人裁决**，见 `docs/plans/day07/d-联调记录.md` L6#10） | 1h | 主理人裁决口径；若裁「协议加字段」需先过契约冻结 | `protocol.ts` 增量字段 + `realtime.ts` 触发分支 + 对应测试 | ① 若裁「加独立路由降级标记」：`tts_text` 增 `route_degraded`（或同语义字段），fallback 轮 pad 显示「网络慢，正在换种方式回答」，正常轮不显示，契约冻结后逐字段校对；② 若裁「维持现状」：仅把 day07 L5-3 预期里提示条项划掉，本任务关闭不写码 |
| H12 | 门禁收口（参与） | 0.5h | 各线完成 | 自检证据 | 见总览 §5 |

> 超载先裁序（R1 顺位核对）：① D5⑤ pre-push 钩子接线（eval_gate 本体+fixtures+归因回流保留）→ ② D3 pad SOS 整项 → 溢出池 O2（H10② 日报 TTS 语音条 + ④ 推送状态前端）。D1/D2/D4/D5 核心为完成定义项，不容顺延。

## 当日时间线

- 09:15 全员评审契约（B1）；10:00 拿到冻结版 → D1 骨架（日报端点结构）
- 10:00–12:00 D1 骨架；**等待 A1/CP1 空档穿插 D5 fixtures 段**（50 例固化+六类边界，不依赖健康线）
- 12:00 **CP1** 后等 C2 → D1 联调段；11:30 拿 A1 模型
- 14:00 X9 交接（B2 桩测试绿）→ **D5 gate 本体开发与首次基线跑测**
- 14:30 **CP2**：与 C1 互测（X3/X4）；D1 收口
- 14:30–16:00 D2 切真 + 375px 走查截图
- **15:30 X5/X7 演练**（家属通知侧证据：四态展示）
- 16:00–16:45 D4 pad 播放模块 + **CP4 戏曲端到端演练 AC-37**（pad 侧证据）
- 17:00–17:30 D5 收尾（归因报告 + bad case 回流；pre-push 接线按 R1② 裁剪判定）
- 17:30 门禁；D3 SOS 仅产能富余才做（R1③）；**溢出池 O2（TTS 语音条 1.5h + 推送状态前端 0.5h，P1）**

## 自检命令

```bash
uv run pytest tests/test_reports_api*.py tests/test_sos*.py -q
uv run python tests/eval_gate.py          # D5：门禁通过即绿；人为调低阈值复跑应拦截
pnpm --dir frontend exec tsc --noEmit
pnpm --dir frontend build                 # family 与 pad 包
# 375px：手机浏览器打开日报页截图，无横向滚动
# pad：戏曲播放/暂停/打断演示录屏或截图
```

## 边界与注意

- 你的写域仅限：`app/api/reports.py`、`app/api/family.py`（增量）、`frontend/src/family/*`、`frontend/src/pad/*`（戏曲播放+SOS）、`eval/fixtures/*`、`tests/eval_gate.py`、对应 tests。不碰 models/迁移/services/graphs/scheduler/rules.py/console/tool_registry/memory_service。
- family 切换层机制已有（N1/N4 先例），严格走 USE_MOCK 单独放行模式，不重造。
- SOS 大按钮组件已存在，只做后端接线与撤销边界，不做新 UI 设计。
- eval_gate 只**读**规则阈值做门禁判定，不得改 rules.py/阈值（阈值归 A5/B 域，X9：失败归因回 B）。
- D4 播放模块只连 media_service 产的稳定 media_url（X8），音频文件替换属运维操作（D-7），不写代码适配层。
