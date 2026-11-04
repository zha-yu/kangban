# 人设 prompt

每种 persona 一个文件（建议 `*.jinja` 或 `*.md`），与 `personas` 表一一对应。

约定（见 AGENTS.md §6、大纲 2.1 提示缓存）：

1. 固定前缀 = system 指令 + 人设 + 工具定义，改动只放在前缀之后；
2. 不含任何真实长者数据、密钥、内网地址；
3. 新增人设需同步 `personas` 表种子数据与评测集（`eval/fixtures/`）。
