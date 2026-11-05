# 教师参考模块（只读，勿改）

教师提供的 asr 参考实现快照，入库供全体成员统一查阅（原路径各人不同：`~/Downloads/asr` 等）。**本目录只读，不得修改、不得 import 进业务代码**——只做移植参照。

## 入库范围与排除项（相对原模块）

| 处理 | 内容 | 原因 |
| --- | --- | --- |
| ✅ 保留 | `src/`、`tests/`、`docs/`、`static/`、`examples/`、`data/`、工程配置文件 | 协议实现、测试样本、教学文档 |
| ✅ 保留 | `funasr-runtime-resources/models/sherpa-onnx/`（41M） | **运行必需资产**：TEN-VAD WASM（Day 3 A4 移植）+ CAM++ 声纹模型（Day 4 spike）+ ten-vad onnx |
| ❌ 排除 | `funasr-runtime-resources/models/damo/`（约 1.7G FunASR ONNX 模型） | 禁区：大模型文件不入库；由 funasr Docker 镜像/网盘分发 |
| ❌ 排除 | `.env`、`key.pem`、`cert.pem` | 密钥与本地证书不入库（仓库 .gitignore 已兜底） |
| ❌ 排除 | `.git/`、`.venv/`、`__pycache__/`、原 `.gitignore` | 非内容物 |

## 对应任务卡

- A1：FunASR 协议参照 `src/asr/providers/asr/funasr_ws.py`、`src/asr/providers/funasr.py`
- A4：TEN-VAD 参照 `src/asr/web/vad/`、`src/asr/web/audio-worklet.js`；WASM 资产在 `funasr-runtime-resources/models/sherpa-onnx/sherpa-onnx-wasm-simd-v2.0.1-ten-vad/`
- A5 / Day 4：声纹 spike 参照 `src/asr/providers/voiceprint*`；CAM++ 模型 `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`（阈值 0.55 是教师口径，本项目硬性数值 0.85，Day 4 重标）
- `.env` 需自建（参考原内容：`FUNASR_URL` 内网地址 + `FUNASR_INSECURE='1'` + `MODEL_DIR`）

快照时间：2026-11-05（源：`/Users/min/Downloads/asr`）
