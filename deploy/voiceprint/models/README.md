# CAM++ 模型文件

从参考项目复制 CAM++ 模型到此目录，容器挂载到 /app/models/。

```powershell
Copy-Item `
  spikes\reference\asr\funasr-runtime-resources\models\sherpa-onnx\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx `
  deploy\voiceprint\models\
```

文件不得入库（`.gitignore` 已排），各队友本地手动放。
