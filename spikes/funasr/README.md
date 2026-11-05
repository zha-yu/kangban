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

## 4. 镜像拉取记录（N7 实测 · 未拉完）

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

### 结论：**未达成 —— 镜像未就绪**（不是「跑通了但指标不好」，是**根本没跑起来**）

| 项 | 实测 |
| --- | --- |
| 容器能否启动 | **否**。`docker images` 里没有 funasr 镜像，`docker compose up -d funasr` 无从起 |
| 镜像拉取 | 两轮共 ≈41 分钟，`timeout 900` 杀过一轮、续拉一轮到 16:44 手动停止，仍未完成（明细见 §4） |
| partial / final 条数 | **无数据**（没有容器，没有出字） |
| 首包 ms / final ms | **无数据** |
| final 文本 | **无数据** |
| `ws_probe.py` 状态 | 代码写完、`--help` 级静态自检通过，但**从未对真容器跑过** —— 因此**不声称它「验证通过」**，只声称「已就绪，待镜像可用后首跑」 |

**没有伪造任何出字结果。** 按计划 `docs/plans/task-a1-ASR与TTS适配层.md` §7 **P-A1-1** 如实登记「未完成 + 原因」，
降级路径由主理人裁决（当日以 Mock + 云端骨架交付，真链路留作待办）。

**后续待办（镜像拉通后按顺序做，不需要改代码）**：

1. `docker pull <镜像>` 拉到 `docker images` 里出现该 tag；
2. `docker compose up -d funasr` → 等 `docker compose ps funasr` 显示 `(healthy)`（首次还要下几 GB 模型，见 §2）；
3. `uv run --with websockets python spikes/funasr/ws_probe.py --text "<期望文本>"`；
4. 把「首包(partial) / final / final 文本」三项填回本表，并删掉这一整段「未达成」结论；
5. 首包若 > 300 ms：**只登记偏差，不改「云 API 为主实现」口径**（计划 §7 P-A1-6、§9-2）。

**首次起容器可能踩的三个坑**（尚未实测，来自参考配方与镜像行为，供后续排查）：

| 现象 | 先查什么 |
| --- | --- |
| 一直 `(unhealthy)` | `docker compose logs funasr`：模型还在下载（几 GB）；`start_period` 只覆盖 180s，慢机器上「先 unhealthy 后 healthy」属正常 |
| 容器起后立刻退出 | 日志里模型名是否 404 / ModelScope 不可达；必要时先去掉 `--lm-dir`、`--itn-dir` 两个可选模型再试 |
| 健康检查报 `python3: not found` | 该镜像若不带 python3，把健康检查换成 `sh -c 'exec 3<>/dev/tcp/127.0.0.1/10095'` 之类；**改这一处不影响服务本身** |

---

## 7. 与 `spikes/reference/asr/compose.yaml` 的差异（故意的，逐条说明）

参考 compose 是老师侧跑通过的配方，但它的形态是「网页 demo + TLS + 10096」。本仓库要的是「本地基线 + 7002 + 纯 ws」，
所以有意做了下面这些改动 —— 不是抄漏了：

| 项 | 参考 compose | 本仓库 | 为什么 |
| --- | --- | --- | --- |
| 镜像 tag | `...-0.1.13` | `...-0.1.12` | 任务卡指定 0.1.12 优先；若拉不动再退 0.1.13（记录在 §4） |
| 宿主端口 | `10096` | `7002` | 7001 给 laya；7002 与 README §6 端口表一致 |
| TLS | 传 `--certfile`/`--keyfile`，客户端 `wss://`，健康检查做 TLS 握手 | **不传**，客户端 `ws://`，健康检查纯 TCP | 官方 `run_server_2pass.sh` 里这两行本来就是注释掉的；本地基线不需要证书信任链（参考侧还得配 `FUNASR_INSECURE=1`） |
| `privileged: true` | 有 | **无** | 参考 README 自述是「保留原 `docker run` 命令的 privileged」，不是能力需求。ONNX CPU 推理不需要特权；`AGENTS.md` §5 把 compose 列为高风险文件，按最小权限起。**若 N8 起不来再按需加回** |
| 模型目录 | 宿主目录 `./funasr-runtime-resources/models` | 具名卷 `funasr_models` | 宿主目录属于 `spikes/reference/`（只读参考，不该被写入模型）；具名卷与 `postgres_data` 风格一致 |
| `--hotword` | 挂 `models/hotwords.txt` | 省略 | 仓库内没有该文件；缺文件可能让服务起不来。热词等有需求再加 |
| 资源限制 | 无 | `deploy.resources.limits.cpus: "2"` | 计划 T1.6 要求限核，别把 postgres/redis 和本地开发抢死 |

---

## 8. 已知限制

1. **无置信度**：FunASR 协议不回 confidence，适配器侧按计划 §3 第 5 条记 `None` + `source`，**禁止填 `0.0` 假值**。
2. **首包预算是按云 API 定的**（PRD 300 ms），本地 CPU 推理超预算属预期，只登记偏差。
3. 探针**只验证到「出字」**，不验证 WebRTC 链路、不验证打断、不验证并发。
