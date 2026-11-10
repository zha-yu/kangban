# Chinese-Jev / Laya 本地 sidecar spike（A2 · S2.0 门禁证据）

> 目的：在写 `app/system_one/**` 之前，用最小脚本回答四个问题——
> ① 权重能不能加载；② 七类意图分不分得开；③ CPU 上要多少毫秒；④ 需要几个问题才守得住 200ms。

## 1. 结论（2026-11-10 实测）

| 项 | 结果 |
| --- | --- |
| **General 权重** | `convaiinnovations/laya-multilingual`（321.9M / F16 / 615 MB / `apache-2.0`，tags 含 `commercial-use`） |
| **推理入口** | 仓库自带 `rl_agent_api.RLAgent.system_one(state, questions)`；**不依赖 pip `laya` 包** |
| G-3 加载 | 通过，`/multilingual` 加载 32 s（CPU, 8 线程），`max_len=1024`、`head_max_len=256` |
| G-4 准确率 | **17/20 = 85%**（门槛 ≥15/20 通过；但 PRD §5.2 要求 50 例 ≥90%，**尚差 5 个点**） |
| G-5 延迟 | **单问 p95 167 ms 达标**；两问 285 ms；四问 668 ms（均 CPU 8 线程） |
| 结论 | 形态 **A 成立**，但 **200 ms 预算只够 1 个问题**——这是 A2/A3 的核心设计约束 |

## 2. 权重与许可（G-1 / G-2 证据）

| 项 | 值 |
| --- | --- |
| HF repo | `convaiinnovations/laya-multilingual`（commit `1720e3e3`） |
| 本地路径 | `D:\models\laya\multilingual`（`.gitignore` 之外，**不入仓**） |
| 体积 | `model.safetensors` 615 MB；仓库总计 ~678 MB；sha256 `9d628fd971b7…95aa8f204` |
| 许可 | `apache-2.0`（model card `cardData.license`），tags 含 `commercial-use` |
| 推理代码 | `D:\models\laya\rl_agent_api.py`（4.7 KB）+ `rl_common.py`（19 KB），来自 `convaiinnovations/laya` 仓库，`apache-2.0` |
| 下载命令 | `HF_ENDPOINT=https://hf-mirror.com hf download convaiinnovations/laya-multilingual --local-dir D:/models/laya/multilingual` |

> 备选证据：`bbldCVer-hf/CJ-Medical`（医学领域，`apache-2.0`，`model.safetensors` 644 MB，
> sha256 `c06cf0bc645fe5c55fb1f478896dfa6b387c3a60d328dde03450bc140ec89732`，与 MANIFEST 一致）
> 已下到 `D:\models\CJ-Medical`，**仅作对照**——它是医学微调版，通用意图路由不是它的目标场景。

## 3. 环境（G-6）

```text
conda env: jev   (D:\Users\Alienware\anaconda3\envs\jev, Python 3.12.16)
torch 2.14.0+cpu | transformers 5.17.0 | safetensors 0.8.0 | numpy 2.5.4
项目 .venv 保持无 torch（未污染）
注：该环境按 CJ-Medical 的 requirements 装过 laya==0.3.11，但**交付代码不 import 它**，仅环境残留。
```

## 4. 测量结果（全部 CPU，`torch.set_num_threads` 指定）

| 配置 | 问题数 | 线程 | p50 | p95 | max | ≤200 ms |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| laya-multilingual | 4（intent+sensitive+privacy+ood） | 8 | 607 | 668 | 716 | ❌ 3.3× |
| laya-multilingual | 4 | 16 | 549 | 619 | 657 | ❌ 3.1× |
| laya-multilingual | 2（intent+sensitive） | 8 | 275 | 285 | 288 | ❌ 1.4× |
| laya-multilingual | 2 | 16 | 249 | 264 | 286 | ❌ 1.3× |
| **laya-multilingual** | **1（intent）** | **8** | **161** | **167** | **170** | ✅ |
| CJ-Medical（对照） | 4 | 2 | 1119 | 1143 | 1158 | ❌ 5.5× |
| CJ-Medical（对照） | 1 | 8 | 162 | 166 | 170 | ✅ |

**读法**：延迟≈与「问题数」线性相关，**加线程几乎不再改善**（16 核也只有 1.3× 提升），说明瓶颈在编码器前向本身，不是调度。

## 5. 20 条明细（laya-multilingual，4 问，8 线程）

| id | 文本 | gold | pred | conf | ms |
| --- | --- | --- | --- | ---: | ---: |
| g01 | 今天心里有点闷，想找个人说说话 | chat | chat | 0.42 | 618 |
| g02 | 我孙子周末要来看我，我挺高兴的 | chat | chat | 0.76 | 548 |
| g03 | 你给我讲个笑话吧，闲着也是闲着 | chat | chat | 0.89 | 568 |
| g04 | 我早上那个降压药吃了没有啊 | medication | medication | 1.00 | 596 |
| g05 | 我的降压药还剩几颗 | medication | medication | 0.27 | 609 |
| g06 | 我吃过药了，不用再提醒我 | medication | medication | 0.89 | 611 |
| g07 | 放一段越剧给我听听 | opera | opera | 0.56 | 607 |
| g08 | 换一段，这段不好听 | opera | **chat** | 0.31 | 668 |
| g09 | 声音小一点，太吵了 | opera | **other** | 0.35 | 618 |
| g10 | 有没有老电影可以看看 | movie | movie | 1.00 | 606 |
| g11 | 放个打仗的片子吧 | movie | movie | 0.99 | 622 |
| g12 | 今天天气怎么样 | weather | weather | 0.99 | 716 |
| g13 | 明天出门要不要带伞 | weather | weather | 1.00 | 609 |
| g14 | 外面冷不冷，我该穿几件 | weather | weather | 0.44 | 623 |
| g15 | 帮我记一下今天的血压 | health_checkup | health_checkup | 0.94 | 595 |
| g16 | 我今天血糖是多少来着 | health_checkup | health_checkup | 0.86 | 597 |
| g17 | 是不是该量体温了 | health_checkup | health_checkup | 0.46 | 602 |
| g18 | 帮我叫一下护工过来 | other | other | 1.00 | 606 |
| g19 | 我想给女儿打个电话 | other | other | 0.57 | 594 |
| g20 | 今天几号了 | other | **weather** | 0.47 | 600 |

**三个错例的共性（重要）**：g08 / g09 依赖「当前正在放戏曲」这个上下文，g20 是「日期」被误归天气。
三者都**不是模型随机错**，而是**题面缺上下文**：
→ 真实链路里 `state` 会带 `current_task`、`media_status`（讲义第 5 课就是这么喂的），预计能救回；
→ 若仍不行，属 `criteria` 措辞问题（A1 的 `questions.py` 要按这 3 条反推描述边界）。
**这条直接决定 A1 的题面设计：`state` 不能只塞 ASR 文本。**

## 6. 复跑方式

```bash
P=/d/Users/Alienware/anaconda3/envs/jev/python.exe
# 单问（200ms 快路径）
$P spikes/chinese_jev_hello/gate.py --model-dir D:/models/laya/multilingual --only intent --threads 8
# 两问（intent + is_sensitive）
$P spikes/chinese_jev_hello/gate.py --model-dir D:/models/laya/multilingual --only intent,is_sensitive --threads 8
# 全四问
$P spikes/chinese_jev_hello/gate.py --model-dir D:/models/laya/multilingual --threads 8
```

产物：`gate_laya_intent.json` / `gate_laya_intent_sens.json` / `gate_laya_full.json` / `gate_laya_2q_t16.json` / `gate_laya_full_t16.json` / `gate_cjmedical.json` / `gate_cjm_intent_t8.json`。

## 7. 待决（需主理人裁决，写进计划 §10）

1. **200 ms 只够 1 问**：快路径定「单问 intent（167 ms 达标）」还是「两问 285 ms + 改口径到 300 ms」？
2. **85% → 90% 的 5 个点**：靠 `state` 带上文 + `criteria` 措辞能不能补上，需要 50 例实测才知道。
3. **GPU 路线**：本机有 RTX 3060 6 GB，但装的是 `torch 2.14.0+cpu`；换 CUDA 版后四问预计 <100 ms。这偏离 TD「CPU 推理」的既有口径，要否立项？

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-10 | 首版：Laya/CJ-Medical 权重与许可证据、环境隔离、7 组延迟实测、20 条明细与 3 个错例归因、复跑命令、三项待决 |
