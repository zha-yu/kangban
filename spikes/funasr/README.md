# spikes/funasr · FunASR 本地 ASR 基线探针

> 归属：Day 3 任务卡 a1 · DAG 节点 **N7 / N8**（见 `docs/plans/task-a1-DAG.md` §3、`docs/plans/task-a1-ASR与TTS适配层.md` §6 T1.6/T1.7）
> 作用域：本目录**只放探针**，不进 `app/`。ASR 适配器（`app/audio/asr.py`）归 asr-core，本目录不 import 它 ——
> 免得把「探针结论」和「实现细节」绑死，也让探针能独立回答「容器到底能不能出字」。

---

## 1. 这个探针干什么 / 不干什么

| | |
| --- | --- |
| **干** | 连 `docker-compose.yml` 里的 funasr 服务，喂 16 kHz PCM，打印 partial / final 与首包耗时 |
| **不干** | 不做重采样、不做 VAD、不做断句、不写库、不产 `ASREvent` —— 那些分别是 A3/A5/A1 的活 |

**为什么不做重采样**：`app/audio` 的帧契约是 16 kHz / 16 bit / 单声道 / 60 ms。探针若偷偷补上重采样，
「重采样写错了」会被记成「FunASR 不认音频」，这条 spike 就白跑了。格式不对**直接报错退出**。

---

## 2. 起容器

```bash
docker compose up -d funasr
docker compose ps funasr          # 等 STATUS 变成 (healthy)
docker compose logs -f --tail 100 funasr
```

- 宿主端口 **7002** → 容器内 **10095**（7001 已登记给 laya，见计划 §4.4/§9-1）。要改端口设 `FUNASR_HOST_PORT`。
- **首次启动慢**：模型按 `--download-model-dir` 下到具名卷 `funasr_models`，几 GB、几分钟。`start_period` 给了 180s，
  超过之后 `docker compose ps` 可能先显示 `(unhealthy)`、模型加载完再翻回 `(healthy)` —— 健康检查失败**不会**自动重启容器，
  属正常现象，重看 `ps` 即可。想从头再来：`docker compose down -v`（会连 `postgres_data` 一起删，慎用）或
  `docker volume rm silvermate_funasr_models`。

---

## 3. 跑探针

```bash
# 仓库内自带 16k 示例音频（spikes/reference/asr/examples/asr_example_zh.wav）
uv run --with websockets python spikes/funasr/ws_probe.py

# 指定音频与期望文本（期望文本只用于人工比对，不参与识别）
uv run --with websockets python spikes/funasr/ws_probe.py --wav my_16k.wav --text "今天天气怎么样"

# 容器换成 wss（自己开了 TLS 才需要）
uv run --with websockets python spikes/funasr/ws_probe.py --url wss://127.0.0.1:7002 --insecure
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--url` | `ws://127.0.0.1:7002` | FunASR runtime 地址 |
| `--wav` | `spikes/reference/asr/examples/asr_example_zh.wav` | 必须 16 kHz / 16 bit / 单声道，否则报错 |
| `--text` | 空 | 期望文本，仅比对用 |
| `--chunk-ms` | `60` | 推流帧长（60 ms = 1920 字节，与 `app/audio` 一致） |
| `--realtime` / `--fast` | `--realtime` | 实时节奏推流 / 尽可能快推完 |
| `--insecure` | 关 | wss 时跳过证书校验（镜像内是自签证书） |
| `--timeout` | `30` | 等 final 的秒数 |

退出码：`0` 拿到非空 final ｜ `1` 连不上 ｜ `2` 超时没等到 final ｜ `3` final 文本为空。

> 探针只依赖 `websockets` 一个第三方包（`uv run --with websockets` 临时装，不进 `pyproject.toml`）。

---

## 4. 镜像拉取记录（N7 实测 · 最终拉完）

| 项 | 结果 |
| --- | --- |
| 镜像 | `registry.cn-hangzhou.aliyuncs.com/funasr_repo/funasr:funasr-runtime-sdk-online-cpu-0.1.12` |
| tag 是否有效 | **有效**。`docker pull` 打印了 `funasr-runtime-sdk-online-cpu-0.1.12: Pulling from funasr_repo/funasr` 并开始逐层下载，不是 404 / manifest unknown |
| 尝试 1 | `timeout 900 docker pull <image>` → **15m0.162s 被 timeout 杀掉，未完成** |
| 尝试 2 | 续拉（前一轮缓存层显示 `Already exists`），16:18 → 16:44 手动停止，约 **25 分钟**，仍未完成 |
| 累计耗时 | ≈ **41 分钟**（16:03 → 16:44），未拉完 |
| 停止时层状态 | 20 个唯一层：**3 层 Pull complete**（已解压）、**9 层 Download complete**（已下载未解压）、6 层 `Already exists`（前一轮缓存）；仍在拉的 5 层：`0f678f3493a4` `3453a7b45018` `70f06463baa0` `91fd1992e8a6` `c6a33d057c5b` |
| 停滞证据 | 拉取日志最后一条写入 16:37:32，之后 7 分钟无新层完成（大层下载慢到肉眼无进展） |
| 最终状态 | `docker images` **无 funasr**；`docker pull` 客户端已停，环境干净（无残留容器/卷） |
| 结论 | **镜像未就绪 → N8 真链路无法进行**（见 §6）。这是环境/网络问题，不是 compose 或探针的缺陷：`docker compose config` 已通过（N7 判据达成） |

> **下次怎么拉得更快**（未验证，按经验排序）：
> 1. 在带宽更好的机器上拉完，再 `docker save` / `docker load` 搬到本机（镜像大且 registry 慢，一次性成本）；
> 2. 配 registry mirror（`/etc/docker/daemon.json` 的 `registry-mirrors`）后重试；
> 3. 换参考 compose 用的 `...-0.1.13` tag（老师侧跑通过，但**没在这里验证过**，不能假设它更快）。
>
> 重拉时**别套 `timeout`、别接 `| tail`**：`timeout` 到点会砍掉拉取，管道会吞掉进度让人误判「卡死」。
> 直接把输出重定向到日志文件（`docker pull <image> > /tmp/pull.log 2>&1 &`），用 `grep "Pull complete"` 数层。

---

## 5. 回退方案（镜像拉不动时）

按计划 `docs/plans/task-a1-ASR与TTS适配层.md` §7 **P-A1-1**：镜像拉不动 → `pip install funasr` + 官方
`funasr_wss_server.py` 自建；再不行 → 当日只用 Mock 交付，FunASR 记「未完成」，**不伪造出字结果**。

自建路径（未在本机验证，仅作配方）：

```bash
# 1) 装 funasr 与 runtime 依赖（CPU 版）
uv pip install funasr modelscope torch torchaudio onnxruntime

# 2) 取官方 runtime 脚本（FunASR 仓库 runtime/websocket/）
#    https://github.com/modelscope/FunASR/tree/main/runtime/websocket
python funasr_wss_server.py \
  --port 10095 \
  --asr_model damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx \
  --asr_model_online damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx \
  --vad_model damo/speech_fsmn_vad_zh-cn-16k-common-onnx \
  --punc_model damo/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727-onnx

# 3) 宿主机直接跑，7002 起一个容器把 10095 映射出去（或用 socat）
docker run --rm -p 7002:10095 -v "$PWD":/w -w /w python:3.12-slim \
  sh -c 'pip install -q funasr modelscope && python funasr_wss_server.py --port 10095 ...'
```

> 注意：这条回退路径**模型与协议与镜像版一致**，探针不用改，只换容器起法。

---

## 6. N8 真链路结论

### 结论：**已达成 —— 真实链路出字**（经项目自己的 `build_asr()` + `ASRProvider.stream()`）

镜像是 19:05 前后拉完的（明细见 §4）。之后**不是**用 `ws_probe.py` 单验，而是直接走仓库代码：

```
provider=FunASRWSProvider url=wss://silvermate-funasr:10095 insecure=True
wav=spikes/reference/asr/examples/asr_example_zh.wav pcm_bytes=177528 frames=93
[partial +15159 ms] 欢迎大
[partial +15294 ms] 家来
[partial +15411 ms] 体验达
[partial +15560 ms] 摩院推
[partial +15724 ms] 出的语
[partial +15893 ms] 音识
[partial +16094 ms] 别模
[final   +47024 ms] 欢迎大家来体验达摩院推出的语音识别模型。
partials=7 final='欢迎大家来体验达摩院推出的语音识别模型。'  probe_exit=0
```

| 项 | 实测 |
| --- | --- |
| 容器能否启动 | **能**。模型加载完成，日志出现 `asr model init finished. listen on port:10095` |
| 连接方式 | **必须 `wss://`**（镜像默认自签 TLS）；宿主直连 `127.0.0.1:7002` 会 `[WinError 1225]`，实际用同 compose 网络 `wss://silvermate-funasr:10095` |
| partial / final 条数 | **7 / 1** |
| 首包 partial | ≈ **15.2 s**（含分段发送 5.55 s 音频的时间） |
| final | ≈ **47.0 s**（2pass-offline 在 CPU 上重跑整段，属预期） |
| final 文本 | `欢迎大家来体验达摩院推出的语音识别模型。`（与 `spikes/reference/asr/examples/asr_example_zh.wav` 内容一致） |
| 首包 vs 300 ms 预算 | **差 50 倍**，只登记偏差：本地基线不承担 PRD 的 300 ms 首包预算（云 API 才承担，见 §8-2） |

**这期间改了两处代码（都已带回归用例）**：

1. **默认走 `wss://` + 新增 `ASR_INSECURE`**：`FUNASR_DEFAULT_URL` 改 `wss://localhost:7002`；`_ssl_context_for()` 按 URL scheme 与 `insecure` 决定是否用宽松校验上下文；`build_asr()` 透传 `config.asr_insecure`。
2. **接收超时从「整段预算」改成「按帧重置的空闲超时」，15 s → 45 s**：首轮真链路在 `+15043 ms` 被误杀（日志报「15.0s 内没有等到终稿」），但其实 partial 一直在出。offline 重跑耗时随音频长度增长，总预算必然误杀正常终稿。

**没有伪造任何出字结果**：以上是原样粘贴的探针输出，`probe_exit=0`。

**复现步骤（一次调用内跑完，别拆成多次）**：

1. `docker compose up -d funasr`；
2. **等日志出现 `listen on port`**——不要等 healthcheck：它只是 TCP 探活，端口通了 server 可能还在加载模型（此时 TLS 握手会挂）；
3. 同网络跑探针（宿主 `127.0.0.1:7002` 在这台机器上连不通，别试）：
   `docker run --rm --network silvermate_default -e PYTHONPATH=/w -e ASR_URL=wss://silvermate-funasr:10095 -v <repo>:/w -w /w python:3.12-slim sh -c "pip install -q websockets pydantic pydantic-settings; python /w/.probe/verify_asr_live.py"`。

**实测踩到的四个坑**：

| 现象 | 真相 |
| --- | --- |
| `docker compose ps` 显示 `(healthy)` 但连不上 | healthcheck 是纯 TCP `socket.create_connection`，**不代表 server 就绪**；加载模型期间连进来会 `TLS handshake failed` |
| `ws://` 报 `did not receive a valid HTTP response` | 镜像**默认开 TLS**（不传 `--certfile` 也开），必须 `wss://` + `ASR_INSECURE=true` |
| 宿主 `127.0.0.1:7002` 连不上（`WinError 1225`） | 端口发布在本机不可用（与 postgres 那次同源），只能走 compose 网络里的容器名 |
| `-v /tmp/x.sh:/x.sh:ro` 之后 `/tmp/x.sh` 变成目录 | WSL 会重启且 `/tmp` 被清空；改用「挂目录」而不是「挂单个文件」 |

---

## 7. 与 `spikes/reference/asr/compose.yaml` 的差异（故意的，逐条说明）

参考 compose 是老师侧跑通过的配方，但它的形态是「网页 demo + TLS + 10096」。本仓库要的是「本地基线 + 7002 + 自签 TLS」，
所以有意做了下面这些改动 —— 不是抄漏了：

| 项 | 参考 compose | 本仓库 | 为什么 |
| --- | --- | --- | --- |
| 镜像 tag | `...-0.1.13` | `...-0.1.12` | 任务卡指定 0.1.12 优先；若拉不动再退 0.1.13（记录在 §4） |
| 宿主端口 | `10096` | `7002` | 7001 给 laya；7002 与 README §6 端口表一致 |
| TLS | 传 `--certfile`/`--keyfile`，客户端 `wss://`，健康检查做 TLS 握手 | **保留镜像默认**（不传参数即 `SSL is opened!`），客户端 `wss://` + `ASR_INSECURE=true`，健康检查纯 TCP | 原以为官方脚本注释掉＝不开 TLS，**实测证明错了**：不传参数镜像仍自签开 TLS（日志 `using TLS mode: Mozilla Intermediate`），`ws://` 直接报 `did not receive a valid HTTP response`。本地基线用自签 + 跳过校验，生产换真证书并关 `ASR_INSECURE` |
| `privileged: true` | 有 | **无** | 参考 README 自述是「保留原 `docker run` 命令的 privileged」，不是能力需求。ONNX CPU 推理不需要特权；`AGENTS.md` §5 把 compose 列为高风险文件，按最小权限起。**若 N8 起不来再按需加回** |
| 模型目录 | 宿主目录 `./funasr-runtime-resources/models` | 具名卷 `funasr_models` | 宿主目录属于 `spikes/reference/`（只读参考，不该被写入模型）；具名卷与 `postgres_data` 风格一致 |
| `--hotword` | 挂 `models/hotwords.txt` | 省略 | 仓库内没有该文件；缺文件可能让服务起不来。热词等有需求再加 |
| 资源限制 | 无 | `deploy.resources.limits.cpus: "2"` | 计划 T1.6 要求限核，别把 postgres/redis 和本地开发抢死 |

---

## 8. 已知限制

1. **无置信度**：FunASR 协议不回 confidence，适配器侧按计划 §3 第 5 条记 `None` + `source`，**禁止填 `0.0` 假值**。
2. **首包预算是按云 API 定的**（PRD 300 ms），本地 CPU 推理超预算属预期，只登记偏差。
3. 探针**只验证到「出字」**，不验证 WebRTC 链路、不验证打断、不验证并发。
