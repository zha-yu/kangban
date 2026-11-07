# B-4 Laya Spike 结论（Day 5 · 2026-11-07）

## 🎯 Spike 目标

验证 Laya（System One）CPU 分类器能否：
1. 在 7001 端口起 HTTP 服务（config.py `laya_base_url` 默认值）
2. 接收 ASR 文本、返回四信号（intent / is_sensitive / confidence / is_out_of_domain）
3. 单句 CPU 推理延迟 ≤ 200ms（AGENTS §6 / §7 硬性数值）

## ✅ 已验证：HTTP 客户端 + Mock 链路 10/10 通过

**不依赖真实 Laya 模型**，用关键词匹配 mock server 模拟 80ms CPU 推理延迟。

```
样本数: 10  成功: 10  失败: 0
延迟(ms): min=119.4  avg=205.0  p95=124.3  max=952.1
≤200ms 达标率: 90.0%（AGENTS §6 硬性目标）
```

**HTTP 请求/响应契约**（与 config.py 对齐，无偏差）：

```
POST http://localhost:7001/classify
Content-Type: application/json

{
  "state": "我要吃药",
  "questions": {
    "intent": {"type": "choice", "choices": ["chat","medication","opera","movie","weather","health_checkup","other"]},
    "is_sensitive": {"type": "score", "min": 0.0, "max": 1.0},
    "confidence": {"type": "score", "min": 0.0, "max": 1.0},
    "is_out_of_domain": {"type": "score", "min": 0.0, "max": 1.0}
  }
}

→ 200 OK
{
  "intent": "medication",
  "is_sensitive": false,
  "confidence": 0.88,
  "is_out_of_domain": false
}
```

## ⚠️ 已知缺口：真实 Laya checkpoint 未加载（进行中）

| 问题 | 原因 | 状态 |
|---|---|---|
| `pip install laya` 能装但**模型权重需额外下载** | laya 0.4.27 自带 ModernBERT-large encoder（421M，英文），第一次启动从 HuggingFace 拉 | ⏳ 下载中（首次约 2-5 min） |
| **TD 1241 明确说大纲首选 Chinese-Jev（中文优化）、候选 Laya** | ModernBERT-large 是英文 encoder，跑中文 intent 分类效果差 | ❌ 当前 laya 包不是最优选型 |
| docker-compose.yml 无 Laya 服务定义 | TD 说 Day 10 再补 | 🔒 等 Day 10 |
| pyproject.toml 无 laya 依赖（spikes 依赖） | spikes 目录独立 | ✅ 已 uv pip install laya 0.4.27 |
| Windows symlinks 警告 | HF Hub 默认用 symlink，Windows 没开开发者模式 | ✅ 降级下载、功能正常 |

## 📋 下一步（Day 6 system_one 封装前置）

1. **等真实 laya-serve 加载完 ModernBERT-large** → 测真实 CPU 单句延迟（首次下载完成后追加到本文件）
2. **评估 Chinese-Jev（中文优化）是否替代 laya PyPI 包**：
   - 如果大纲/主理人裁决 Chinese-Jev 必须上 → spikes/laya_hello/ 换成 spikes/chinese_jev_hello/
   - 如果继续用 laya PyPI 包 → 用 `laya-multilingual`（mmBERT-base，100+ 语言、2x faster）替代默认 ModernBERT-large
3. **加载本项目七类 intent checkpoint**：当前 laya 默认 checkpoint 是通用英文分类，需要训练/下载 intent/sensitive/confidence/privacy 四信号的 checkpoint
4. **system_one/ 封装**：Day 6 在 `app/system_one/` 写 Python 客户端，按本文 HTTP 契约调 7001 端口

## 📁 Spike 文件

| 文件 | 作用 | 合并主线？ |
|---|---|---|
| `spike_hello.py` | httpx 客户端 + 延迟测量 + 固定测试集 | ❌ |
| `mock_laya_server.py` | Python stdlib http.server + 关键词匹配 mock | ❌ |
| `README.md` | 本结论 | — |

## ✅ 已装依赖（spike 级，不入 pyproject）

```
laya==0.4.27        # PyPI 包作者 Convai Innovations（Apache-2.0）
torch==2.15.0
transformers==5.22.2
```

## 🔴 Windows 安装坑位（Day 6 system_one 部署参考）

| 坑 | 现象 | 根因 | 处置 |
|---|---|---|---|
| uv cache 被清理软件移走 | pip install 中途 exit_code=-1、cache 进回收站 | Windows Defender 或清理工具误删 `.uv/cache/` | `uv cache clean laya && uv pip install --no-cache-dir laya` |
| `.venv\.lock` 被后台任务占 | 另一个 `uv run` 等待锁、超时 | 两个 `uv run` 不能同时用同一个 .venv | 等前一个退出 或 手动删 lock |
| HF Hub symlink 警告 | UserWarning: symlinks not supported | Windows 没开开发者模式 | 降级下载、功能正常；或开开发者模式 |
| pip.exe 找不到 | `C:\Python313\Scripts\pip.exe` 不存在 | 本项目用 uv 管理 venv，不用 pip | 用 `uv pip install` 或 `uv run python -m pip install` |
