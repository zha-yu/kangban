# task-1 · Step0 身份与权限契约冻结

| 项 | 内容 |
| --- | --- |
| 状态 | ✅ 已完成（2026-11-04） |
| 认领人 | A–E 全员 |
| 主责 | A–E 全员 |
| 前置 | 无（**现在可认领**） |
| 写作用域 | `docs/接口契约_身份与权限.md`（新建） |

## 目标

开工前 40 分钟把所有接口与载荷定死，后续不再改。这是整天的「契约闸门」：不定死就各写各的。

## 产出

新建 `docs/接口契约_身份与权限.md`，必须写清五块：

1. **端点清单与请求/响应 schema**
   - `POST /auth/login`、`POST /auth/refresh`、`GET /auth/me`、`POST /auth/device/register`
   - `POST | GET | PATCH /elders`
   - `POST /devices/{id}/bind`、`GET /devices/{id}/elder-candidates`
   - 技术设计文档没有 API 章节，**路径与字段由本任务冻结**，后续 Step1–5 一律以此为准。
2. **token payload 三体系**
   - 人：Access 2h + Refresh 7d，载荷含 `ver = token_version`
   - 设备：24h，载荷含 `device_id`
   - 工牌（P1）：短时效 + `scope`
   - 算法与过期必须与 PRD §3.1（line 180）一致，不许自己改数
3. **角色 → 权限矩阵**
   - 照 PRD §3.1（line 172-178）落成「角色 × 操作」判定表
   - 注意：`staff_accounts.role` 只有 `admin / care_worker / doctor`；**家属在 `guardian_accounts` 独立表**，不是 role 枚举值（技术设计文档 line 355/361）
4. **错误码约定**：401 凭证无效 / 403 越权 / 404 不存在 / 409 幂等或唯一冲突
5. **四项裁决结论留痕**：D1 登录标识、D2 设备首次接入、D3 双人间候选字段、D4 工牌 token 范围（见 `README.md` §四）

## 禁止触碰

**读：必须读**——本文的字段与判定全部来自这三份文档，不读就写不出来。
**改：可以改，但不许静默改**——任何口径变化必须同一次提交内在对应修订记录里补一条（AGENTS 禁区 7）。

## 验收

5 人逐条过签；后续任何任务与契约文件冲突时，以契约为准改代码，改契约需全组同意。

## 交付记录（2026-11-04）

- 产出：`docs/接口契约_身份与权限.md`（461 行），提交 `707bdf6 docs(contract): 冻结 Iter 1 身份与权限接口契约`。
- 实际落地的端点名与上方「产出」第 1 条**不一致**：`/auth/login`→`POST /api/sessions`、`/auth/refresh`→`POST /api/sessions/refresh`、`/auth/me`→`GET /api/sessions/current`、`/auth/device/register`→`POST /api/device-sessions`。原因：旧名照抄大纲 Day 2 课时，按 AGENTS 铁律 6 不得作为口径；路径全部标为自裁 S-01 / A-03。
- 四项裁决已结：D1→S-02、D2→S-03、D3→S-06、D4→A-04（见契约 §8 / §9）。
- 另加 6 条自裁（S-05、S-07~S-10）与 3 条裁决（A-01 家属档案读权限、A-02 审计降级、A-03 路径前缀）。
