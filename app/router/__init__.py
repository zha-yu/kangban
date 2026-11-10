"""三级路由：`safety.py`（G0 安全一票否决）→ `rules.py`（≤10 条高频固定意图）→ System One → 主控 Agent。

顺序不可调换：安全永远先于一切，规则先于分类器，分类器先于大模型。
阈值与规则表都写在代码里（`RULESET_VERSION` / `system_one_confidence_threshold`），不靠 prompt 自觉。

Day 7 起本包承担三件事（互不越界）：
- `safety.py`：G0，与分类器无关，否定/历史/他人/模糊四类边界在此裁决；
- `rules.py`：零延迟固定意图，命中即出 `RuleHit`，**不调模型**；
- `decision.py`：组装 `RouteDecision`，负责 G1（分类器敏感信号 → 复用同一安全升级口）与
  "低置信只澄清一次"；直连工具一律经 `ToolExecutor.execute()`（G2），不在此处复制权限判断。
"""
