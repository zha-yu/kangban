> 状态：未开始　｜　负责人：dev-C（全栈 · 健康域 API、media_service 与 console 三页）　｜　日期：2026-11-11（Day 8）　｜　版本：v1.1（v1.0＋C4）
> 依据：docs/plans/day08/day-08-健康采集与三端后台.md v1.2 ｜ 对接点与全局节奏见同目录《day08-任务总览与DAG.md》

# dev-C 个人任务书

## 任务清单

| # | 任务（对应计划） | 人时 | 前置 | 交付物 | 验收标准（量化） |
| --- | --- | --- | --- | --- | --- |
| C1 | H5：健康域 REST（骨架先行） | 2.5h | B1（冻结契约）；A2（联调段，X1） | `app/api/health_*.py` + `tests/test_health_api*.py` | ① 代填提交（仅负责长者）/记录查询复核（黄护工✅红❌/医生✅）/复核确认与纠错新版本，字段逐项对齐冻结契约；② 医生趋势查询端点（近 14 天序列）；③ 全走 ContractAPIRoute，操作人从 JWT 注入，范围过滤在 services；④ 经 ROUTERS 注册表挂载（不碰 main.py）；⑤ 越权三例 403+审计；新增接口测试全绿 |
| C2 | H4：report_service 日报生成 | 2.5h | A2；X2 口径（11:30 与 A 对齐） | `app/services/report_service.py` + `tests/test_reports_service*.py` | ① 采集完成或 20:00 强制结束生成首版，未答项「未采集」；② 内容仅来自结构化记录/用药结果/关注事件，**不读 memory_chunks**（grep 验证）；③ 用药四类分列进 medication_stats（AC-13 前半）；④ 补录新版本保留历史、每版只发一次更新通知（dedupe_key，AC-25）；⑤ 固定声明「教学模拟信息，不构成医疗诊断」 |
| C4 | H14②：media_service（媒体唯一写库层） | 1.5h | A4（0018 表就绪）；X8 冻结口径 | `app/services/media_service.py` + `tests/test_media_service*.py` | ① 列表/按剧种剧目搜索/取播放 URL/history 写入接口测试绿；② media_url 只产 X8 稳定路径约定 `/static/media/opera/<media_id>.wav`；③ history 写入含播放时长/完播率（FR-CON-06 P1）；④ 服务内不直接碰 Tool 层（Tool 归 B4） |
| C3 | H9：console 真实对接（P0 部分①–④） | 3h | C1、D1 | `console/{health,reviews,alerts}-api.ts` 切真 + 三页面修正 + 走查截图 ×3 | ① 代填 165/85→黄色复核任务出现、来源=代填、含操作者（AC-32）；② 护工 A 看不到 B 楼栋数据（AC-18 前半）；③ 告警中心未读/确认/状态展示对齐 X7 四态；④ 看板红档置顶核对（X6，接口已交付补排序核对）；⑤ 导出签名不变、tsc 零错 |
| H12 | 门禁收口（参与） | 0.5h | 各线完成 | 自检证据 + 三页截图归档 | 见总览 §5 |

> 超载先裁序（R1 顺位核对）：C1–C4、C3 均为健康主线/欠债核心，**无包内先裁项**；溢出池 O1（H9⑤ 医生 14 天趋势图，P1）17:00 未开工即顺延 Day 9。

## 当日时间线

- 09:15 全员评审契约（B1）；10:00 拿到冻结版后 C1 骨架开工（类型/ROUTERS/错误体）
- 11:30 与 A 对齐 X2（dedupe_key 口径）
- 11:30 后 A4 迁移就绪 → C4 可穿插开工（media_service 不依赖健康线，可在等 CP1 空档先写骨架）
- 12:00 **CP1**：A2 签名冻结 → C1 联调段（真实 service 接入）+ C2 开发
- 14:30 **CP2**：C1/D1 互相联调通过（X3）
- 14:30–15:45 C4 收口（接口测试 + media_url 稳定路径核对）
- 15:45–17:15 C3 三页切真 + 走查截图
- **15:30 X5 演练**（告警中心侧证据）；**16:00 CP4 戏曲端到端演练 AC-37**（media_service 侧证据：列表/搜索/URL）；16:30 X6 看板核对留截图
- 17:30 门禁；**溢出池 O1（H9⑤ 趋势图前端，1h，P1）**

## 自检命令

```bash
uv run pytest tests/test_health_api*.py tests/test_reports_service*.py tests/test_media_service*.py -q
pnpm --dir frontend exec tsc --noEmit
grep -n "memory_chunks" app/services/report_service.py   # 零命中
```

## 边界与注意

- 你的写域仅限：`app/api/health_*.py`、`app/services/report_service.py`、`app/services/media_service.py`（v1.1 扩域）、`frontend/src/console/*`、对应 tests。不碰 models/迁移/graphs/scheduler/tool_registry/memory_service/rules.py/family/pad。
- 前端只按冻结契约开发，发现字段缺失走契约 §6 留痕改版，不得私改（X3）。
- console 维持 React + 原生 CSS（D-3 裁决），不引 UI 框架。
- media_service 是媒体域唯一写库层：B4 的 Tool、A5 的规则直连、D4 的 pad 播放都只经它取数/写 history，你不暴露绕过它的写入口。
