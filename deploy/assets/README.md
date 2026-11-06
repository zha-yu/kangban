# 3D 静态资产（`deploy/assets/`）

Day 4 · A3 的 3D 展示链路只依赖这一份模型文件。**资产不进 Git**（`.gitignore` L74–75 已忽略
`*.vrm` / `*.glb`），进仓库的只有本文与占位用的 `avatars/.gitkeep`。

## 1. 落盘路径 ↔ URL

| 落盘路径 | 容器内路径（nginx `alias`） | 对外 URL 前缀 |
| --- | --- | --- |
| `deploy/assets/avatars/` | `/usr/share/nginx/html/assets/avatars/` | `/avatars/` |

- 一次性验证容器把 `deploy/assets` 挂到 `/usr/share/nginx/html/assets`，于是
  `/avatars/vrm1_sample.vrm` 命中的就是 `deploy/assets/avatars/vrm1_sample.vrm`；
- 库里 `personas.avatar_url` 存**相对路径**（`/avatars/vrm1_sample.vrm`），响应层再拼
  `PUBLIC_BASE_URL` 成绝对 URL（C7 §3.1；实现见 `app/services/persona_service.py`）；
- `deploy/` 不在技术设计文档 §2.1 的目录约定里，属 A3 自裁路径（计划 S-04），只占
  `deploy/nginx.conf` 与 `deploy/assets/` 两处，不改动 `app/`、`frontend/`、`seeds/`。

## 2. 当前文件与体积上限

| 文件 | 体积 | 校验 |
| --- | --- | --- |
| `avatars/vrm1_sample.vrm` | **10,776,032 字节**（10.3MB） | VRM **1.0**；前 4 字节 `glTF` |

**单个 VRM 上限 15MB**（T4 验收项）：超过后首屏 10MB 级模型的加载时间会挤掉
FR-3D-02 的「失败或超 5 秒降级静态头像」窗口，前端来不及在 5 秒内决定要不要降级。
体积不够就压缩贴图，不要靠调大 5 秒阈值绕过去。

## 3. 从哪来、怎么放

源站（2026-11-06 实测可达，three-vrm 官方示例）：

```
https://pixiv.github.io/three-vrm/packages/three-vrm/examples/models/VRM1_Constraint_Twist_Sample.vrm
```

取文件 + 两项校验（PowerShell，仓库根执行）：

```powershell
$p = 'deploy/assets/avatars/vrm1_sample.vrm'
Invoke-WebRequest -Uri 'https://pixiv.github.io/three-vrm/packages/three-vrm/examples/models/VRM1_Constraint_Twist_Sample.vrm' -OutFile $p
(Get-Item $p).Length                                                                # 10776032
[Text.Encoding]::ASCII.GetString((Get-Content $p -AsByteStream -TotalCount 4))       # glTF
```

**失败源（勿再找）**：

- `raw.githubusercontent.com`：本机 TLS 握手失败（`SSL connection could not be established`）；
- `https://pixiv.github.io/three-vrm/packages/three-vrm/examples/models/AvatarSample_A.vrm`：**404**，该文件不存在。

换正式形象（阶段二）：VRoid Studio 做「小满」导出 **VRM 1.0** → 放
`deploy/assets/avatars/xiaoman.vrm` → 只改一列：

```powershell
C:\Python313\Scripts\uv.exe run python scripts/seed_personas.py --avatar-url /avatars/xiaoman.vrm
```

零代码改动；`/api/pad/avatar-config` 的 `version` 指纹会随 `avatar_url` 变化，前端据此重取模型。
VRM 1.0 不需要旋转补偿（`personas` → `avatar_rotation_y_offset = 0.0`，计划 S-02）。

## 4. 为什么不入库、新 clone 怎么补

- 规则出处：`.gitignore` L74–75（`*.vrm` / `*.glb`）＋ AGENTS §4.11「媒体大文件不进普通提交」；
- 复核：`git check-ignore -v deploy/assets/avatars/vrm1_sample.vrm`
  → 应输出 `.gitignore:74:*.vrm	deploy/assets/avatars/vrm1_sample.vrm`；
- 因此**新 clone 后本目录为空**，按 §3 重下即可；`avatars/.gitkeep` 只负责保住目录本身
  （否则 `-v "$PWD/deploy/assets:/usr/share/nginx/html/assets:ro"` 会挂出一个不存在的路径）。
- 资产缺失不是阻塞项：接口与 nginx 验收都能跑，只是前端 `<model-viewer>` 拉 404 后走静态头像降级
  （计划 §5 R2）。

## 5. 本地验证（一次性容器，不进 compose）

前提：Docker 守护进程在跑。容器只验证「URL → 文件 → Content-Type」这一段，不参与联调部署。

```powershell
docker run --rm -p 8080:80 `
  -v "$PWD/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro" `
  -v "$PWD/deploy/assets:/usr/share/nginx/html/assets:ro" `
  nginx:alpine

# 另开一个终端
curl -sI http://127.0.0.1:8080/avatars/vrm1_sample.vrm | Select-String -Pattern 'content-type'
```

验收：`200` + `Content-Type: model/gltf-binary`（`.vrm` 的 MIME 由 `deploy/nginx.conf` 的
`types { model/gltf-binary vrm; }` 提供；写成 `octet-stream` 时文件能下载但这条断言不过）。

联调期把 `.env` 的 `PUBLIC_BASE_URL` 指到 `http://127.0.0.1:8080`——`https://localhost`
在没有自签证书时拉不到资产（计划 §5 R1），nginx 也不进 compose（A-01 裁决）。
