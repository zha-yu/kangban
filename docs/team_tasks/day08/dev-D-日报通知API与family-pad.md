> 负责人：dev-D（全栈 · 日报通知 API 与 family/pad）　｜　日期：2026-11-10（Day 8）　｜　总人时 ≈ 7.5h
> 依据：docs/plans/day08/day-08-健康采集与三端后台.md v1.1 ｜ 对接点与全局节奏见同目录《day08-任务总览与DAG.md》

# dev-D 个人任务书

## 任务清单

| # | 任务（对应计划） | 人时 | 前置 | 交付物 | 验收标准（量化） |
| --- | --- | --- | --- | --- | --- |
| D1 | H6：家属日报 + 通知域 REST 收口 | 2h | B1（冻结契约）；A1（DailyReport 模型）；C2（联调段） | `app/api/reports.py`、`app/api/family.py` 增量 + `tests/test_reports_api*.py` | ① 日报列表/详情接口（家属仅绑定长者，AC-13/18 后半）；② 通知已读/人工确认接口收口（staff 侧确认走 alerts 域）；③ 推送状态查询接口（成功/失败/重试/手动重发为管理员能力）；④ 家属 A 访问长者 B 日报 403+审计；已读幂等；ContractAPIRoute + ROUTERS 挂载 |
| D2 | H10：family 切真 + 375px 验收（P0 部分①③） | 2.5h | D1 | `family/data-source.ts` F0/F1/F2 切真 + 走查截图 | ① 按 `day05/d-家属端切换层说明.md` 流程，Mock 删除条件不满足的保留开关；② 375px 无横向滚动截图留证、正文 ≥16px、按钮够大、高对比度；③ 家属仅见绑定长者日报；④ 日报文本与接口一致、含固定声明 |
| D3 | H11：pad SOS 接线 + 撤销边界（P1） | 2h | A3（敏感事件+工单链路） | `SosButton` 后端接线 + 撤销接口边界 + `tests/test_sos*.py` | ① 点击即生成敏感事件+护工工单，不经语音路由（FR-ALT-06）；② SOS 按下 30 秒内护工看板出现红色置顶工单（与 C 联演留证）；③ 普通确认 10 秒可撤销+审计；红色事件撤销尝试仅追加「说错了」、事件保持 open（AC-26 口径）；④ 按钮 ≥48×48px |
| H12 | 门禁收口（参与） | 0.5h | 各线完成 | 自检证据 | 见总览 §5 |

## 当日时间线

- 09:15 全员评审契约（B1）；10:00 拿到冻结版 + 11:00 等 A1 模型 → D1 骨架（日报端点结构）
- 11:30 后 D1 骨架继续；12:00 **CP1** 后等 C2 → D1 联调段
- 14:30 **CP2**：与 C1 互测（X3/X4）；D1 收口
- 14:30–16:30 D2 切真 + 375px 走查截图
- 15:30 X5/X7 演练（家属通知侧证据：四态展示）
- 16:00–17:30 D3 SOS 接线（P1；若 D2 超时，D3 让位并登记顺延）
- 17:30 门禁；**溢出池 O2（H10② 日报 TTS 语音条 1.5h + ④ 推送状态前端 0.5h，P1）**

## 自检命令

```bash
uv run pytest tests/test_reports_api*.py tests/test_sos*.py -q
pnpm --dir frontend exec tsc --noEmit
pnpm --dir frontend build    # family 包不含 pad 依赖
# 375px：手机浏览器打开日报页截图，无横向滚动
```

## 边界与注意

- 你的写域仅限：`app/api/reports.py`、`app/api/family.py`（增量）、`frontend/src/family/*`、`frontend/src/pad/*`（SOS 相关）、对应 tests。不碰 models/迁移/services/graphs/scheduler/console。
- family 切换层机制已有（N1/N4 先例），严格走 USE_MOCK 单独放行模式，不重造。
- SOS 大按钮组件已存在，只做后端接线与撤销边界，不做新 UI 设计。
