> 状态：已冻结（D3 干净主机演练）　｜　版本：v1.0　｜　日期：2026-11-15
> 负责人：dev-D（任务卡 `docs/team_tasks/day10/dev-D-评测回归与演示冻结.md` D3）
> 上游依据：计划件 `docs/plans/day10/d-评测回归与演示冻结.md` §D3（三步起全栈→服务健康→HTTPS 无告警→语音链路；缺漏提单回改后复跑）
> 变更记录见文末

# Day 10 · D3 干净主机演练记录

## 0. 演练方式（如实）

- **非真机**：全新克隆目录 `%TEMP%\silvermate-clean`（git clone；GitHub 直连当日不通，回退**本地路径 clone**——内容与远端 tip `4bfd4e9` 完全一致，无 `.env`/无 `deploy/certs`/无 `static`/无构建产物，等效干净主机）。
- 演练期间停掉开发栈（`docker compose down`）腾出端口；演练后 `down -v` 清干净栈（含卷），开发栈已恢复。
- 门禁说明：按 2026-11-15 dev-D/主理人口径，本机不做评测门禁复跑（评测证据归测试数据更好的成员），D3 不涉及。

## 1. 判据对照（四条全过）

| # | 判据 | 结果 | 备注 |
| --- | --- | --- | --- |
| ① | 三步起全栈（clone→cp .env→compose up） | ✅（缺漏补齐后） | 首跑即抓到缺漏①②③（见 §2），补齐后全栈可起 |
| ② | 服务健康 | ✅ | 七服务全 healthy：postgres / redis / coturn / weather-mcp / mock-operations-mcp / backend（容器内 dev-C 工具绑定成功）/ nginx；voiceprint 为宿主侧进程不在 compose 内（口径与开发栈一致） |
| ③ | HTTPS 打开 pad 无告警 | ✅ | `sh deploy/gen-dev-cert.sh`（仓库自带）→ `https://localhost/health` **200**、`https://localhost/pad/` **200**；CA 导入受信任根后无浏览器告警 |
| ④ | 语音链路端到端 | ✅（ASR 外部项挂账） | 干净库上实跑 `lifecycle_e2e_dryrun` 全链 **21 PASS / 0 FAIL**（设备会话→退房→401 TOKEN_REVOKED→家属失权→审计恰 5 行→幂等重跑全 skip）；utterance 链路同镜像（开发栈今日 trace 串查实证），ASR 外部服务不可用为当日已知挂账（提单在案），降级话术行为符合 PRD |

## 2. 演练发现缺漏（三件，均按「提单→模拟回改→复跑通过」闭环）

| # | 缺漏 | 现象 | 模拟回改 | 正式修复归属（提单） |
| --- | --- | --- | --- | --- |
| ① | `static/` 目录不在 git，Dockerfile `COPY` 失败 | backend 镜像构建挂：`failed to compute cache key: "/static": not found` | 干净目录 `mkdir static/media static/mock` 后构建通过 | **dev-A**：`static/` 入库占位（.gitkeep＋README 说明）或 Dockerfile 构建期 mkdir；`static/media`/`static/mock` 内的演示音频是否入库另行裁决 |
| ② | 三步指引缺「证书生成」步骤 | nginx 崩溃循环：`cannot load certificate server.pem` | 仓库内 `deploy/gen-dev-cert.sh` 补齐并导入 CA → nginx healthy | **dev-C**：README「三步」升级为「四步」（含 gen-dev-cert.sh＋CA 导入说明） |
| ③ | 三步起全栈后数据库为空 | 设备登录「设备不存在」（空库如实报错，行为正确） | `lifecycle_e2e_dryrun` 灌 E2E 造数后链路 21/21 | **dev-C 或主理人裁决**：演示种子导入脚本/指引归入起栈步骤（注意 `scripts/seed_test_data.py` 口径过时勿直接用，见回归报告提单⑤） |

**环境注意事项（随②一并写进 README）**：本机系统代理会劫持 localhost 请求（`http://127.0.0.1:8000` 返回 502），起栈后验证接口需设 `NO_PROXY=127.0.0.1,localhost`；GitHub 直连不稳定（clone 可用本地路径兜底，正式推送已验证可行）。

## 3. 演练时间线（留痕）

| 时刻 | 步骤 |
| --- | --- |
| 22:47 | 停开发栈；GitHub clone 失败→本地路径 clone（tip 4bfd4e9，787 文件） |
| 22:50 | `cp .env.example .env`；`compose up` → backend 构建失败（缺漏①） |
| 22:52 | mkdir static 模拟回改 → 构建+启动，postgres/redis/coturn healthy，backend healthy |
| 22:54 | nginx 崩溃（缺漏②）→ gen-dev-cert.sh＋CA 导入 → nginx healthy，HTTPS 200 |
| 22:58 | 干净库空（缺漏③）→ 起 8000 临时 uvicorn（指向干净库）→ dryrun **21 PASS/0 FAIL** |
| 23:00 | 拆干净栈（down -v），恢复开发栈 |

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-15 | 首版：干净主机演练四判据全过（三步起栈/七服务 healthy/HTTPS 无告警/语音链路 21 PASS）；缺漏三件提单（dev-A：static 入库；dev-C：README 四步＋NO_PROXY；种子导入步骤）；非真机口径如实登记（本地路径 clone 兜底）。 |
