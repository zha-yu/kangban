# KWS（唤醒词）wasm 资产重建方法

`frontend/public/kws/` 下的产物（~17MB）不进 Git，需要时按下述步骤重建。
2026-11-06 W0 spike 结论：sherpa-onnx 官方不发布浏览器 KWS wasm 预编译产物（npm 包为 Node 专用），
必须本地编译。wasm 运行时与模型均为 int8 优化，浏览器端运行，音频不出端（AGENTS 红线 10）。

## 依赖

- emsdk **4.0.23**（脚本硬校验；macOS 下 emsdk 自带 python 解包可能失败，
  可 curl 下载 `downloads/python-3.13.3-0-macos-arm64.tar.gz` 后手动
  `tar -xzf ... -C python/3.13.3_64bit --strip-components 1`）
- cmake（brew 或 pip `--user` 均可，或用 Kitware 官方 universal 二进制包）

## 步骤

```bash
git clone --depth 1 https://github.com/k2-fsa/sherpa-onnx.git
cd sherpa-onnx
# 模型（int8 mobile 版，共 4.5MB）：解压后把 4 个文件放进 wasm/kws/assets/
#   encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx
#   decoder-epoch-12-avg-2-chunk-16-left-64.onnx
#   joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx
#   tokens.txt
# 下载地址：https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/
#   sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01-mobile.tar.bz2
source <emsdk>/emsdk_env.sh
./build-wasm-simd-kws.sh          # 产物在 build-wasm-simd-kws/install/bin/wasm/
cp build-wasm-simd-kws/install/bin/wasm/sherpa-onnx-wasm-kws-main.{js,wasm,data} \
   wasm/kws/sherpa-onnx-kws.js <repo>/frontend/public/kws/
```

唤醒词「小伴」不改模型词表，由 `frontend/src/pad/kws/kws-detector.ts` 以内联
keywords（`x iǎo b àn @小伴`）传入。加载失败自动落「呼叫按钮」降级（R-5），不影响其他功能。
