# FunASR Docker Compose

在本文件所在的项目根目录运行命令。配置文件为 `compose.yaml`，模型目录为 `funasr-runtime-resources/models`。

## 首次从手动容器切换

当前手动创建的容器也叫 `funasr`，占用 10096 端口。先停用并保留旧容器，再启动 Compose：

```powershell
docker stop funasr
docker rename funasr funasr-manual-backup
docker compose up -d --wait --wait-timeout 300
```

旧容器仅作为备份保留，模型文件仍使用同一个宿主机目录。已有同名备份时，请为备份选择其他名称。若没有旧容器，直接执行最后一条命令。

## 日常操作

```powershell
# 启动；容器启动后会自动启动 ASR，无须进入容器执行命令
docker compose up -d
# 查看状态，模型加载完成并通过 TLS 检查后显示 healthy
docker compose ps
# 查看实时日志；Ctrl+C 只退出日志查看
docker compose logs -f --tail 100 funasr
# 重启识别服务
docker compose restart funasr
# 停止并移除 Compose 容器，宿主机模型文件保留
docker compose down
```

保持 WSS 地址：`wss://127.0.0.1:10096`，与当前网页版配置一致。使用镜像内置证书。网页后端在本地测试时继续设置 `FUNASR_INSECURE=1`，正式部署应使用可信证书。

## 启动行为

镜像内的 `run_server_2pass.sh` 会把识别程序放到后台并退出。本配置直接以前台方式启动同一个 `funasr-wss-server-2pass` 程序，并传入你提供的所有模型、热词和 WSS 参数，使程序退出状态可以由 Docker 管理。无需 `nohup`、`&`、`-it` 或 `tail -f`。日志直接进入 `docker compose logs`，每份最大 20 MB，保留 3 份。

`restart: unless-stopped` 在程序退出后自动重启；手动停止的容器保持停止。Docker 本身仍需启动。健康检查只报告本地 TLS 是否就绪，不会因 unhealthy 自动重启容器。首次下载模型和加载模型可能超过几分钟，此时可查看日志，等待完成后再次检查。`--wait-timeout` 超时不表示容器已经停止。

默认解码线程为 4，模型线程和 IO 线程为 1；可按机器资源调整。以下配置改变时用 `up -d` 更新：

```powershell
$env:FUNASR_PORT = '10096'
$env:FUNASR_DECODER_THREADS = '4'
docker compose up -d
```

热词文件为 `funasr-runtime-resources/models/hotwords.txt`，可以留空，或每行填写 `热词 权重`。修改热词后重启服务生效。保留了原命令的 `privileged: true`。

Compose 字段说明参考 [Docker 官方文档](https://docs.docker.com/reference/compose-file/services/)。
