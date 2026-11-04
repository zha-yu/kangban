"""三级路由的 L0：`safety.py` 安全一票否决 + `rules.py` 高频固定意图（≤10 条，Day 6）。

规则层必须排在 Laya 与主控 Agent 之前，阈值写在代码里而不是 prompt 里。
"""
