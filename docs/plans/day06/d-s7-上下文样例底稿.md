> 状态：已完成（供 dev-B N22 引用）　｜　版本：v1.0　｜　日期：2026-11-09
> 关联：docs/plans/day06/dev-b-状态中心与上下文记忆.md（B-7.2 上下文样例作业）｜app/core/context.py（N06 实现）｜docs/plans/day06/d-s4-20轮验证记录.md（D1 缺陷）
> 上游依据：技术设计文档 N06 ContextBuilder（实现）｜讲义 §7.1 七层顺序、§7.3 预算、§7.5 边界标签（口径）
> 变更记录见文末

# S7 · 上下文样例底稿（dev-D，真实装配产出）

> **性质**：本底稿是 dev-B N22 B-7.2「上下文样例」的**真实数据底稿**——不是手写示意，是 2026-11-09 直接调用 `app/core/context.py :: build_turn_context()` 的真实返回。dev-B 写 `dev-b-作业三件套.md` 时可直接引用本文数据并补齐「逐层为什么」叙述，不必再虚构样例。
> **复现**：种子数据见 §1；脚本（临时件）`%TEMP%\kangban-s4\ctx_sample.py`，命令 `uv run python <脚本>`。

## 1. 真实数据种子（可复用，勿删）

| 项 | 值 |
| --- | --- |
| 长者 | 张桂芬 `11111111-1111-4111-8111-111111111102`（device 绑定，display_name=张桂芬） |
| thread | `11111111-1111-4111-8111-111111111102:13`（pad 实测会话） |
| memory_chunks | 3 条测试记忆，`source_thread_id='test-seed-context-sample'` 标记（越剧偏好 0.90 / 孙女探望 0.85 / 忌口 0.95，importance 0.90/0.80/0.85） |
| 历史 | 该 thread 今日真实 5 轮（user_text 有值，`assistant_delivered_text` 全 NULL——见 §5 发现①） |
| now | 传入 `2026-11-09T15:00Z`（展示层 23:00 Asia/Shanghai） |

## 2. 场景 A · 正常轮（device 身份 + 3 条记忆 + 用药待答任务）

`budget_used: 704 / 4000，degraded_layers: []`。messages 实际顺序与内容（`role` 逐条，真实返回全文）：

```
[0] system  固定前缀 FIXED_SYSTEM_PROMPT（小满人设 + 医疗边界 + ASR 容错说明，逐字节稳定保 cache）
[1] system  当前说话人身份：已确认。
            对长者的称呼：张桂芬。
[2] system  当前时间：2026-11-09 23:00。
[3] system  【当前任务进行中】任务类型：medication_confirmation。等待长者输入：taken_answer。
            请优先结合用户本轮输入判断是否为该任务的回答。
[4] system  <retrieved_knowledge kind=authoritative_fact source=memory_chunks id=66cac8d2-… version=0.90>
            长者喜欢越剧，尤其爱听《梁山伯与祝英台》，常要求重复播放
            </retrieved_knowledge>
            <retrieved_knowledge kind=authoritative_fact source=memory_chunks id=7d5a114b-… version=0.95>
            长者不吃辣，饭菜偏好清淡软烂
            </retrieved_knowledge>
            <retrieved_knowledge kind=authoritative_fact source=memory_chunks id=79e95ef0-… version=0.85>
            长者的孙女小雨每周六下午来探望，聊到孙女时情绪明显变好
            </retrieved_knowledge>
[5] system  <retrieved_knowledge kind=memory source=memory_chunks id=66cac8d2-… version=0.90>…同上三条…</retrieved_knowledge>
[6] system  【最近对话摘要】
            - 长者说：这个太死了
            - 长者说：没说这个
            - 长者说：一人
            - 长者说：我得出来点流水
            - 长者说：可以，这行吗。
[7] system  【任务绑定提示】如果本轮用户的回答像是对用药确认（吃了/还没吃/不想吃/吃了半片）的回应，
            优先将用户输入解释为对该用药确认任务的答复。
[8] user    这个太死了
[9] assistant  null          ← 真实返回即 null（D1 后果，见 §5①）
[10] user   没说这个
[11] assistant null
[12] user   一人
[13] assistant null
[14] user   我得出来点流水
[15] assistant null
[16] user   可以，这行吗。
[17] assistant null
[18] user   我昨天在花园里种的花开了     ← 本轮输入
```

**逐层映射**（讲义 §7.1 七层 ↔ 实际 message 序号）：

| 层 | must_keep/can_trim | 对应 [n] | 估算 token | 备注 |
| --- | --- | --- | --- | --- |
| safety_and_policy | must_keep | [0] | ~153 | 固定前缀，独立 system 消息 |
| identity_and_scope | must_keep | [1] | ~9 | generic 时整层换 GENERIC_SYSTEM_PROMPT |
| runtime_info | can_trim | [2] | ~9 | now=None 时整层为空、不产出消息 |
| active_task | must_keep | [3] | ~34 | N05 StateSnapshot.task 注入 |
| authoritative_facts | can_trim | [4] | ~130 | 按 importance 取前 3，带 §7.5 边界标签 |
| relevant_memories | can_trim | [5] | ~130 | **Day 6 桩：与 [4] 同源同内容重复**（见 §5③） |
| older_summary | can_trim | [6] | ~60 | **Day 6 桩：输出的是原文列表不是摘要**（见 §5②） |
| （独立于七层） | 不参与裁剪 | [7] | ~40 | waiting_for 绑定，waiting_for=taken_answer 的专用文案 |
| 历史 | 不参与裁剪 | [8]-[17] | — | 最近 5 轮 × (user+assistant)，`_assemble_history_turns` |
| 本轮输入 | — | [18] | — | 单独追加一次 |

## 3. 场景 B · generic 轮（未确认身份对照）

`budget_used: 225 / 4000，degraded_layers: []`。差异：

- [1] 换成 GENERIC_SYSTEM_PROMPT（不得称呼姓名/引用记忆/确认用药/写健康数据）；
- **[4][5][6] 三层全部为空**（generic 强制忽略记忆与历史）；
- [8]-[17] 历史消息全部不出现；
- 保留 [3] active_task 与 [7] 任务绑定提示（用药确认任务仍在）→ **见 §5④ 语义矛盾发现**；
- 无称呼行、无[2]之外的个人信息。

## 4. 场景 C · 预算裁剪演示（`ContextBuilderConfig(budget_tokens=120)`）

真实返回：`budget_used: 177 / 120`，`degraded_layers: ["older_summary", "relevant_memories", "authoritative_facts", "runtime_info"]`。

- 裁剪顺序 = `_CONTEXT_PRIORITY_TAIL_FIRST`（尾优先）：older_summary → relevant_memories → authoritative_facts → runtime_info，逐层放不下即整层丢弃并记入 degraded_layers；
- must_keep 三层（[0][1][3]）+ waiting_for 绑定 + 历史 + 本轮输入全部保留；
- **`budget_used(177) > budget_total(120)`**：must_keep 层与历史不参与裁剪，预算被 must_keep 突破时照实上报、不强行截断——这是代码现行语义，B-7.2 引用时请如实标注。

## 5. 真实产出暴露的四个现状（dev-B 引用前必读）

| # | 现状 | 影响 | 归属 |
| --- | --- | --- | --- |
| ① | **历史 assistant 消息 content=null**：`conversation_turns.assistant_delivered_text` 全 NULL（S4 缺陷 D1），`_assemble_history_turns` 组装出 `{"role":"assistant","content":null}`。OpenAI 兼容接口对 `content:null` 的 assistant 消息可能直接报错或被忽略——**D1 不只丢审计，实际会污染每一次带历史的 LLM 请求** | 上下文链路 | dev-C（D1 修复后自动消失；可加 `or ""` 防御但根因在落库） |
| ② | older_summary 输出的是**原文列表**（每条 80 字截断）不是摘要——`history_service` 摘要生成未实现，桩直接列原文 | 标注即可 | dev-A/dev-B（N22 标注「桩形态」） |
| ③ | authoritative_facts 与 relevant_memories **同源重复**（N06 注释已声明：N08 召回骨架 Day 6 桩复用同一查询），3 条记忆出现两遍 | token 浪费 ~130 | dev-B（N08 正式实现后替换） |
| ④ | **generic 轮保留了用药任务的 active_task + 绑定提示**：GENERIC_SYSTEM_PROMPT 说「不得确认用药」，[7] 却引导模型「优先解释为用药确认答复」——两段 system 语义互相矛盾，generic 身份不应收到个人任务的绑定提示 | 语义矛盾 | **建议 dev-B N22 评估**：`_assemble_active_task`/`_assemble_waiting_for_binding` 是否应加 `identity.is_generic` 短路（改动极小，属 N06 实现域） |

## 6. 与 B-7.2 验收的对照

B-7.2 要求「12 轮历史 + 3 条记忆 + 2 条知识 + 1 个待答任务，逐条说明送进的/被裁的/为什么」：

- 3 条记忆 + 1 个待答任务：本文 §2 真实覆盖 ✅；
- 「2 条知识」：Day 6 无知识库表，[4] 的 authoritative_fact 即事实注入形态，建议 dev-B 以本文数据替代并注明差异；
- 「12 轮历史」：代码上限 5 轮（`DIALOGUE_MAX_ROUNDS=5`，10 条消息），**不存在 12 轮形态**——B-7.2 的作业口径应按代码 5 轮改写，不要虚构 12 轮；
- 被裁的内容：§4 场景 C 真实演示 ✅。

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-09 | 首版：三场景真实装配产出（正常轮 704/4000、generic 对照、budget=120 裁剪演示）、七层↔消息序号映射表、四个现状发现（assistant content=null 污染 LLM 请求【D1 影响面升级】/ 摘要层实为原文列表 / 记忆层同源重复 / generic 保留用药任务提示的语义矛盾）、与 B-7.2 验收对照（12 轮口径应改 5 轮） |
