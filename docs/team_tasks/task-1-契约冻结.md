# task-1 · Step0 身份与权限契约冻结

| 项 | 内容 |
| --- | --- |
| 状态 | ⬜ 待认领 |
| 认领人 | — |
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

`docs/` 下 PRD / 技术设计文档 / 大纲正文。若确需改口径，必须同一次提交内的修订记录留痕（AGENTS §4.7）。

## 验收

5 人逐条过签；后续任何任务与契约文件冲突时，以契约为准改代码，改契约需全组同意。
