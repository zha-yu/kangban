"""ASR Mock 脚本夹具包。

放在包内（而不是 `tests/`）是刻意的：`MockASRProvider` 是 `ASR_PROVIDER` 未配置时的默认实现，
运行时必须能读到脚本；`tests/` 不进生产镜像，放在那里等于给默认配置埋一颗 `FileNotFoundError`。

见 `app/audio/fixtures/asr_script.py` 的模块 docstring。
"""
