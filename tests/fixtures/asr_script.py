"""兼容层：Mock 脚本正文已迁到 `app/audio/fixtures/asr_script.py`。

改动原因见包内那份文件的 docstring（`mock` 是默认 provider，运行时不能依赖 `tests/` 存在）。

这里保留同名常量只为两件事：
1. 阅读方便 —— 在 `tests/` 下能直接看到夹具长什么样；
2. 老用例若 `import tests.fixtures.asr_script` 不至于断。

**不要在这里改脚本内容**：`load_mock_script()` 按 AST 读包内文件，改这里不生效。
"""

from __future__ import annotations

from app.audio.fixtures.asr_script import ASR_EMPTY_SCRIPT, ASR_MOCK_SCRIPT

__all__ = ["ASR_EMPTY_SCRIPT", "ASR_MOCK_SCRIPT"]
