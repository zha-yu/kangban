# Day 9 覆盖率报告（D3.5 · TD:1173 接口 ≥70%）

> 命令：`uv run pytest tests/ -q --cov=app --cov-report=term`
> 结果：**TOTAL 80%**（13490 语句，2361 未覆盖，3136 分支中 366 未覆盖）——**≥70% 达标**
> 测试：**1449 passed**（2026-11-12 实跑 5m37s；含 D1/D2 交付的 test_client_receipt.py 9 条＋test_outbound.py 6 条）
> 明细：`eval/reports/day9_coverage_full.txt`（term 汇总尾部）

## 结论

| 判据 | 期望 | 实测 | 结果 |
| --- | --- | --- | --- |
| 接口覆盖率（TD:1173 / FR-TC-06） | ≥70% | **80%** | ✅ 达标 |
| 全量测试 | 全绿 | 1449 passed | ✅ |

## 低覆盖模块备注（不属于本判据缺口，供 Day 10 排期参考）

- `services/medication_plan_service.py` 23%：药单管理 CRUD 主链在 day08 dev-a-药单管理后端接口计划件，其接口测试未跑 cov 全量口径
- `services/auth_service.py` 45%：token 三体系深层分支（吊销/轮换）由 test_security/test_gates 覆盖，cov 汇总口径下分支判定偏低
- `scheduler/medication_jobs.py` 39%：调度 tick 的休眠/重试路径依赖时序 mock，Day 7 联调已人工取证
- 上述三项为**已知口径差异**，不是闸门 E 判据项；如需对齐请主理人裁决口径后再调
