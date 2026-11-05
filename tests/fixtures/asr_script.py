"""ASR Mock 固定脚本（A1 · N2 · 计划 §T1.1/§T1.2）。

为什么是「数据文件 + `ast.literal_eval`」而不是 `import`：

- `app/` 反向 import `tests/` 会破坏分层单向（AGENTS §2），容器里也根本没有 `tests/`；
- 所以 `app/audio/asr.py::load_mock_script()` 只**按固定路径读文件**，用 `ast.literal_eval`
  取常量值 —— 不执行、不 import，取到的仍是 Python 字面量；
- 本文件因此只放常量数据与注释，**不写逻辑**（逻辑放 `asr.py`，别在这里分叉）。

两个脚本的分工：

| 常量 | 用途 |
| --- | --- |
| `ASR_MOCK_SCRIPT` | 默认脚本：`PARTIAL × 2 → FINAL`，给 A2/A3/A5 断网联调与 pytest 全链路 |
| `ASR_EMPTY_SCRIPT` | PRD FR-UI-05「没听清」：终稿空文本。适配器**不得造词**，判定归 A2/A5 |

改这里等于改 Mock 的对外行为：`ASR_MOCK_SCRIPT` 的文本、条数、先后顺序都会被下游用例观测到。
"""

# 默认脚本：两句中间结果 + 一句终稿（`is_final=True` 之后不会再有事件）
ASR_MOCK_SCRIPT: list[dict[str, object]] = [
    {"text": "今天", "is_final": False},
    {"text": "今天天气", "is_final": False},
    {"text": "今天天气怎么样", "is_final": True},
]

# 「没听清」脚本：终稿是纯空白（`.strip() == ""`），用来证明适配器原样返回、不加工不造词
ASR_EMPTY_SCRIPT: list[dict[str, object]] = [
    {"text": "", "is_final": False},
    {"text": "   ", "is_final": True},
]
