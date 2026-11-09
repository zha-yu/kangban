> 状态：已完成（与 N07 实现已对齐，1 个开放点留 S4）　｜　版本：v1.4　｜　日期：2026-11-09
> 关联：docs/team_tasks/day06/contracts.md（v1.1 §4.2 / §5.1）｜docs/plans/day06/dev-a-历史与工具执行计划.md（A05/A06）｜docs/plans/day06/day-06-长者侧Agent闭环.md（N07/N14）
> 上游依据：PRD v2.9 FR-UI-04、AC-08｜技术设计文档 v2.0 §5.3/§8.3｜ER v0.5 §4.3.12
> 交付方向：dev-A（历史层）→ dev-C（响应流 N07 + 统一出口 N14）

# 交接件 · `segment_ack → delivered_text 截断 → commit_response`

> 这份是 A 给 C 的**接线说明**，不是新契约。字段以 `contracts.md` v1.1 为准；本文只讲
> 「什么时候调、传什么、谁写哪条事件」——这三件事弄错，历史层会记下错的「已送达」。

## 0. 一句话结论

**中途 ack 只更新状态（Redis/内存），不调 `commit_response`；一次回复结束时调一次。**
`commit_response` 是**幂等**的（按 `(turn_id, response_id)` 只生效一次），每段都调的话
第一段就把结论定死了，后面被打断的截断信息再也写不进去。

## 1. 事件归谁写（最容易写重的地方）

| 事件 | 谁写 | 时机 |
| --- | --- | --- |
| `response_started` | **dev-C** | 首段下发前，`append_event(event_type="response_started", response_id=...)` |
| `response_segment_delivered` | **dev-C** | 收到该段 `status=played` 的 ack 后，一段一条 |
| `response_interrupted` | **dev-C** | 打断生效（停声 + 清队列）之后 |
| `response_completed` | **响应层 N07 先写**（末段 played / 打断时）；未接线时由 `commit_response` 兜底 | 一次回复结束时 |

> ✅ **2026-11-09 更正（v1.4）**：N07 已按契约 §3 先写 `response_completed`，这是对的。
> 冲突不在「谁写」，而在 `commit_response` 原先看到事件已存在就 `return` —— 那样投影
> 永远为空，上下文里每句助手回复都会消失。**已改**：事件存在时只补投影、不重复写事件。
> 双方都不必再改口径。

## 2. 调用时序

```
首段下发前        append_event(response_started)                     ← C
第 1 段 played    append_event(response_segment_delivered, idx=0)    ← C
                  + 更新 state.output.last_acked_segment=0          ← C（Redis，不落库）
第 2 段 playing   只更新 last_acked_segment，**不落 delivered**        ← C
用户打断          停声 → 清队列 → append_event(response_interrupted)
                  → commit_response(status="interrupted")           ← C 调 A
自然播完          commit_response(status="completed")              ← C 调 A（内部写 response_completed）
最后              finish_turn(status="completed"|"interrupted")      ← C 调 A
```

`commit_response` 与 `finish_turn` 的顺序建议固定为 **先 commit、后 finish**：
`finish_turn` 只写轮次终态与 `ended_at`，它不读 `delivered_text`，先调也不会错，
但固定顺序能让事件流水里 `response_completed` 永远在 `turn_completed` 之前，复盘时好读。

## 3. 截断规则（这是本条链的核心）

`delivered_text` 的定义：**长者可听见的文本 = 所有 `status=played` 的段，按 `index` 升序拼接。**

| ack 状态 | 是否计入 delivered | 说明 |
| --- | --- | --- |
| `playing` | **否** | 正在播但没播完，插话时这句长者只听到一半，算"听过"会让他以为你说完了 |
| `played` | **是** | 唯一计入的状态 |
| `canceled` | **否** | 带的是**上一个成功播放段**的 `segment_id`，用于确定截断点，本身不计入 |

三条具体规则：

1. **按 `index` 拼，不按到达顺序拼**。ack 会乱序（网络抖动、多段并行下发），按到达顺序
   拼出来的句子是乱的，而这句话要直接进下一轮上下文。
2. **截断点取 `last_acked_segment`（只由 `played` 推进）**，`canceled` 携带的
   `segment_id` 只做一致性校验：两者不一致时**以水位为准**并打点告警，不要用 ack 里的值
   去补内容——那个值本身已经在 delivered 里了。
3. **一段都没播完就被打断**（`canceled` 时 `segment_id` 缺省或 -1）：`delivered_text`
   传 `None`，不要传空串。空串会让投影里出现一条"说过话但一个字都没说"的轮次，
   上下文构建时还会被当成有效回复。

## 4. 幂等与重复 ack

- 同一段收到多次 `played`（重连重放）：`commit_response` 层面无害（幂等），但
  `response_segment_delivered` 会多写几条。**C 侧按 `last_acked_segment` 单调过滤即可**
  （契约 §4.2 的第 ④ 步校验本来就是干这个的）。
- 迟到的 ack（上一代 `epoch`）：四步校验里第 ② 步淘汰，**不要**把它算进 delivered。
  只校验 `epoch` 不够，同代内打断后的追认要靠 `status`（契约 §4.2 已写明）。

## 5. 送给 A 的入参

```python
await commit_response(
    session,
    turn_id="turn-abc",
    response_id="resp-1",
    generated_text="今天晴，二十度。风不大，适合下楼走走。",   # 生成全文（进事件 payload）
    delivered_text="今天晴，二十度。",                        # 只填已播完的段；一段没播完填 None
    status="interrupted",                                     # completed / interrupted / failed
)
```

- `generated_text` 与 `delivered_text` **必须分开传**。前者进
  `conversation_events.payload_json`（审计用），后者进 `conversation_turns`（上下文用）。
  合成一个参数，历史层就无法区分"说过"与"播过"。
- `status` 只写进事件 payload，**不改** `conversation_turns.status`——轮次终态统一由
  `finish_turn` 写。两个写口会让迟到的 `completed` 覆盖已发生的 `interrupted`。
- 抛错语义：`turn_id` 不存在抛 `HistoryError`。**不要吞掉**——让 `turn_service` 兜底
  记 `turn_completed(status=failed)` 并打点，静默吞掉会让"这轮说了什么"永久缺失。

## 6. generic 模式（身份未确认）

`conversation_turns` 没有投影行（`elder_id IS NULL` 时不建）。此时：

- `commit_response` 会抛 `HistoryError`（找不到 turn）。**不要**为了绕过去构造假 turn。
- 正确做法：generic 轮次只写事件，`delivered_text` 丢弃——因为没有任何长者会读到它。
- `finish_turn` 对 generic 是安全的：它会用 `turn_id` 回查 thread 并补一条
  `turn_completed(payload.projection="none")`。

```python
if runtime.elder_id is None:
    await append_event(..., event_type="response_completed", payload={...})  # 仅留审计
    await finish_turn(session, turn_id=turn_id, status=status)
else:
    await commit_response(...)
    await finish_turn(...)
```

## 7. 自检用例（C 可以直接抄）

| # | 场景 | 期望 |
| --- | --- | --- |
| 1 | 3 段，播完 2 段后被打断 | `assistant_delivered_text` = 前 2 段拼接；第 3 段不在 |
| 2 | 同上，重复上报第 2 段 `played` 两次 | delivered 不变；`response_completed` 仍只有 1 条 |
| 3 | 第 1 段 `playing` 中打断（一段没播完） | `delivered_text is None`；`turn.status='interrupted'` |
| 4 | 旧 `epoch` 的 ack 迟到 | 被拒且**不**计入 delivered；`last_acked_segment` 不回退 |
| 5 | 同 `response_id` 再调一次 `commit_response` | 完全无副作用（幂等），不新增 `response_completed` |
| 6 | generic 会话 | 无投影行；`read_turns` 返回空；事件流仍有 `turn_completed` |

命令：`uv run pytest tests/test_history_store.py -q` 已覆盖 1/2/5/6 的 A 侧行为；
C 侧只需补「ack 乱序」「epoch 淘汰」两条，验证 `delivered_text` 的取值。

## 8. C 的回复（2026-11-09 15:40）与一处必须先解的冲突

C 已确认两点（要点照录）：

1. **逐段写 `segment_delivered`**：是。时机 = 收到该段 `played` 后（契约 §3 的触发条件），
   每段一条事件；下发/ack 另打 metrics（复用 `record_stage`）——那是可观测，不是事件流水。
2. **中途 ack 入 Redis**：是。`playing` / `played` 走完整四步校验，通过后更新
   `last_acked_segment`（初始 `-1`）；`canceled` 只走 ①②③、跳过 ④，不推进单调水位。

> 第 1 点与本文一致，无需再动。**第 2 点里有一句和契约打架，必须先解决。**

### 8.1 冲突 P1：同一个水位承担了两个语义（已裁决，见 §8.3）

「`playing` 也更新 `last_acked_segment`」与契约 §4.2 第 ④ 步「`segment_id > last_acked_segment`」
互相否定：

1. 段 2 开始播 → `playing(2)` 通过 ④，水位从 1 推到 2；
2. 段 2 播完 → `played(2)` 到达，④ 判 `2 > 2` 为假 → **被当成迟到 ack 丢弃**；
3. 结果：没有任何段被标记过「播完」，`delivered_text` 永远是 `None`。
   —— 正常播完的一轮也会被记成「一个字都没说」。

把 ④ 改成 `>=` 只能绕过第 2 步，却留下更要紧的问题：水位变成**两个**语义
——「最高见过的段」（去重）与「最后整段播完的段」（截断）。打断时两者必然分叉：
用户在第 2 段播到一半插话，最高见过 = 2，实际听完 = 1。用前者算 `delivered_text`，
就会把只播了一半的第 2 段算成「听过」——正是本文件开头反对的那件事。

### 8.2 两个可选解

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| **A（推荐）** | 水位**只由 `played` 推进**；`playing` 只做 ①②③ 并打点，不碰水位 | 改动最小：④ 原样成立，一个字段一个含义；`canceled` 天然不改水位 |
| B | 保留 `playing` 推进，另加 `last_played_segment`（只由 `played` 推进）专供截断 | 多一个字段，B 的 `StateSnapshot.output` 与 Redis 结构都要加，两个水位日后极易混用 |

选 A 之后，截断规则可以写死成一行，不需要任何额外标记：

```python
# last_acked_segment 语义 = 最后"整段播完"的段；-1 表示一段都没播完
delivered_text = (
    None
    if last_acked_segment < 0
    else "".join(segment_text[i] for i in range(last_acked_segment + 1))
)
```

`last_acked_segment == -1` 同时就是「一段没播完」的判定条件——第 3 节规则 3 不用另设哨兵值。

### 8.3 裁决结果：C 选方案 A（2026-11-09 15:52）

**水位只由 `played` 推进；`playing` 只做 ①②③ + 打 metrics，不推进；`canceled` 也不推进。**

C 给的三条理由（照录，均可回查契约）：

| # | 理由 | 结论 |
| --- | --- | --- |
| 1 | 语义一致性：计划 C-5 与契约都定死「`delivered_text` 只拼 `played` 段」，所以水位的语义必须是「最后播完段」 | 若 `playing` 也推进，同段随后的 `played` 被 ④ 判迟到 → `delivered_text` 恒为 `None`；改 `>=` 又会把「播了一半被打断的段」算成听过 |
| 2 | `canceled` 不推进与 v1.2 自洽：`canceled.segment_id` = 最后一个已确认 `played` 段的 id（无则 `-1`） | 它**回显水位**，不推进水位，两端同构（这一点已补进契约 §4.2.1） |
| 3 | 零契约变更 | 不加字段、不改 §4.2 四步校验，不需要再升版本；`playing` 走 ①②③ 并打 `response_segment` 指标，顺带产出首段开播延迟 |

**本仓契约已同步到 v1.2**（`docs/team_tasks/day06/contracts.md` §4.2.1）：C 提到「v1.2 已定」，但仓库里的副本还停在 v1.1——两份口径不能并存，所以按 C 的定义补齐了字段语义、`-1` 哨兵与截断公式。**零字段变更**，只是把原本口头的规则写死。

#### 一条必须写下来的不变量

`delivered_text` 的公式依赖 **④ 的严格单调**：

```python
# 合法，因为 ④ 保证 [0..wm] 每一段都收到过 played
delivered_text = None if wm < 0 else "".join(seg[i] for i in range(wm + 1))
```

**若日后有人把 ④ 从 `>` 放宽成 `>=`，或允许 ack 跳号（比如 0、2 而没有 1）**，
上面这行会静默地把没收到的段拼进去——不报错、不告警，只是上下文里多了一句他没听过的话。
改 ④ 时必须同步改截断算法，两条一起改、一起测。

### 8.4 给 dev-D 的一句话（不需要改代码）

```
【口径确认】ack 三态的时间点不变，你那边不用改代码：
- playing：开播即报（只用于首段开播延迟指标）
- played ：该段**真正播完**再报（服务端只认这一种推进水位）
- canceled：打断时上报，segment_id 填**最后一个已播完的段**；一段都没播完填 -1
```

`played` 提前上报（比如"开始播就报 played"）会让服务端把没播完的段算成听过，
这是这条链上唯一需要前端守的纪律。

### 8.5 开放点：「末段轨结束」怎么判（dev-D 留到 S4）

dev-D 已落地转达件③，只留一个开放点：**最后一段的「播完」以什么为准**。

**为什么这不是纯前端细节**：末段播完时 `<audio>.ended` 可能因尾静音、TTS 尾部 padding
或音频上下文悬挂而晚到甚至不来。若末段 `played` 因此丢失，服务端水位停在 `total-2`，
一条**正常播完**的回复会在历史里被记成「少说一句」——而这句恰好是"记得吃药"这类
关键信息的落点。

影响面（两边各一件，别混为一件事）：

| 侧 | 影响 |
| --- | --- |
| dev-A | `commit_response` 只接字符串，**不阻塞**；但 V3 用例 1（三段播完）会少一段 |
| dev-C | `response_completed` 是否发出取决于「队列排空」信号，与水位判定是两件事 |

**建议规则（需 C 确认 `completed` 的触发信号）**

| 结束方式 | `delivered_text` 取值 | 理由 |
| --- | --- | --- |
| `status="completed"` | **按 `total` 全覆盖**，不依赖末段 ack | completed 的语义就是「客户端队列已排空」；再用水位会把末段丢掉 |
| `status="interrupted"` | 按 `last_acked_segment` 截断 | 打断只能以「最后一段**完整**播完」为界 |

三个前提，缺一不可：

1. `completed` 必须由**客户端队列排空 / 末段 `ended`** 触发，**不能**由服务端超时推断
   —— 超时很可能是网络断了，那时实际并没播完，用 `total` 就是高估。
2. `total` 取服务端下发 `tts_text.total`，**不能由客户端数 ack 得出**（丢一个 ack 就少一段）。
3. dev-D 的 S4 若确认 `ended` 不可靠：用**末段时长 + 100ms 兜底定时器**上报该段 `played`，
   而不是干脆不报。宁可晚 100ms 上报，也不要让末段永远缺 ack。

**给 A 的一句约束**：`completed` 分支若按 `total` 拼装，请**整段按 total 拼**，
不要出现「前几段按水位、末段按 total」的混用——截断点一旦取决于实现顺序，
同一个场景两次跑会得到不同的 `delivered_text`，历史就不可复算了。

## 8.6 合流后实测（2026-11-09 19:20，B/C 代码已进 develop）

把 develop 的 B/C 代码合进来后，A 侧实测出**三处真实缺陷**，均已修复：

| # | 现象 | 根因 | 处置 |
| --- | --- | --- | --- |
| 1 | 上下文里一条历史都没有 | `app/core/context.py` 按契约 §5.1 调 `read_turns(thread_id=..., elder_id=...)`，而我的实现把 `session` 写成必填 → `TypeError`，且 B 只 catch `ImportError`，直接炸 | `read_turns` / `read_events` 的 `session` 改为**可选**（缺省自开只读会话），并补一条签名回归用例 |
| 2 | 每句助手回复都从上下文消失 | 响应层先写 `response_completed`（契约 §3 的时机），`commit_response` 查到事件已存在就直接 `return`，投影永远是 `None` | 事件已存在时**只补投影**、不重复写事件；新增用例模拟「N07 先写、commit 后到」 |
| 3 | 用药层还挂着 `sa.table()` 过渡 | B 的 `app/models/medication.py` 已合入（E1 偿清） | `medication_service` 切到 ORM，子图与调用方零改动（当初把映射关在一个文件里的收益） |

另外确认**没有冲突**的两处：C 的 `ToolExecutor` 协议（`execute(tool_name, args, runtime, turn_id, idempotency_key)`）与我的实现签名一致；`state_service` 的 `last_acked_segment` 只由 `played` 推进，与契约 §4.2.1 的 v1.2 口径一致。

## 9. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.4 | 2026-11-09 | **与 N07 实现合流后更正事件归属**：`response_completed` 归响应层先写（契约 §3），`commit_response` 在其已存在时只补投影；新增 §8.6 记录合流实测的三处缺陷（`session` 必填、投影被幂等短路、`sa.table()` 过渡已偿清）与两处确认无冲突项 |
| v1.3 | 2026-11-09 | dev-D 回执：转达件③已落地，仅留「末段轨结束」一个开放点（S4）。新增 §8.5：说明末段 `played` 丢失会让正常回复被记成「少说一句」；给出 completed 按 `total`、interrupted 按水位的分流规则与三个前提（completed 不得由超时推断、`total` 取服务端下发、`ended` 不可靠时用「时长+100ms」兜底）；并约束 A 侧不得混用两套拼装 |
| v1.2 | 2026-11-09 | **P1 裁决（方案 A）**：新增 §8.3 记录 C 的三条理由与结论「水位只由 `played` 推进」；把「④ 严格单调是截断公式的前提」写成显式不变量与变更风险；新增 §8.4 给 dev-D 的一句话（无需改代码，只需守住 `played` 的上报时机）；本仓契约同步到 v1.2（§4.2.1） |
| v1.1 | 2026-11-09 | 记录 C 的两点回复（逐段写 `played` 事件 + `canceled` 跳过 ④）；据此发现并写入**冲突 P1**（`playing` 也推进水位会让 `played` 被 ④ 拒、且打断时水位语义分叉），给出方案 A（水位只由 `played` 推进，推荐）与 B（加 `last_played_segment`），并把截断规则收敛成一行；§3 规则 2 改为「截断点取水位，ack 值仅做一致性校验」 |
| v1.0 | 2026-11-09 | 首版：事件归属表（`response_completed` 归 `commit_response` 独占）、调用时序、截断三规则、幂等与迟到 ack、generic 分支、6 条自检、2 个待 C 确认项 |
