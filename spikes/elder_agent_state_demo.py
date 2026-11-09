"""Day 6 · N20 可运行最小实验：验证状态版本、打断和历史投影（讲义 §12）。

只依赖 Python 标准库，不修改任何生产代码。验证三件事：

1. ASR partial 只覆盖草稿，不进正式历史（`user_finals` 只收 final）；
2. 旧 epoch 的迟到播放确认被拒绝，不污染新状态；
3. 上下文投影只使用实际送达文本（`delivered_text`），不含未播完的部分。

讲义口径说明：讲义 §12 的迟到回调校验用的是 **epoch + status 双条件**
（`epoch != self.epoch or response.status != "playing"`），不是「回调序号」。
按「讲义与指令冲突以讲义为准」执行，回调序号不在本实验的状态机里。

当场改动题（讲义 §12 末尾）：把迟到回调传入的 epoch 从 `reply.epoch`（旧代次）
改为 `session.epoch`（当前代次），观察它仍因 `status != "playing"` 被拒绝。

    为什么生产系统必须同时校验 epoch 与 status（契约 §4.2 同口径）：

    - `epoch` 负责「跨代淘汰」：打断后新回复开新代次，旧代次的回调即使侥幸
      带上了"看起来是当前"的代次号（客户端时钟/重连后回读当前 epoch），也不能
      把旧回复的内容追认进新代次的状态；
    - `status` 负责「同代内打断后的追认拦截」：只校验 epoch 时，同一代次内
      已被打断的回复仍可能被后续迟到的 ack 追认——本实验的当场改动正是这条：
      epoch 校验通过（传的就是 session.epoch），仍必须靠 `status != playing`
      把它拦下；
    - 两条是"与"关系，缺一不可：只查 epoch 会同代追认（本实验场景），
      只查 status 则防不住跨代复用同一 response_id 的旧回调（r1 的 status
      尚未翻成 interrupted、而会话已进入新代次的乱序窗口）。

运行：`uv run python spikes/elder_agent_state_demo.py`，任何一步不符即 assert 失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Response:
    """一次回复：生成文本 + 实际送达的分段 + 所属代次 + 播放状态。"""

    response_id: str
    epoch: int
    generated_text: str
    delivered_segments: list[str] = field(default_factory=list)
    status: str = "playing"

    @property
    def delivered_text(self) -> str:
        return "".join(self.delivered_segments)


@dataclass
class Session:
    """会话状态：版本号、代次、ASR 草稿、正式历史、回复集合。"""

    version: int = 0
    epoch: int = 0
    asr_partial: str = ""
    user_finals: list[str] = field(default_factory=list)
    responses: dict[str, Response] = field(default_factory=dict)

    def update_partial(self, text: str) -> None:
        # partial 只覆盖当前草稿，不追加到正式历史。
        self.asr_partial = text

    def commit_final(self, text: str) -> None:
        self.user_finals.append(text)
        self.asr_partial = ""
        self.version += 1

    def start_response(self, response_id: str, generated_text: str) -> Response:
        response = Response(response_id, self.epoch, generated_text)
        self.responses[response_id] = response
        self.version += 1
        return response

    def acknowledge_segment(self, response_id: str, epoch: int, segment: str) -> bool:
        response = self.responses[response_id]
        # epoch 不一致说明它来自被打断的旧回复，不能污染新状态；
        # status != playing 拦下同一代次内已被打断回复的迟到追认（当场改动题场景）。
        if epoch != self.epoch or response.status != "playing":
            return False
        response.delivered_segments.append(segment)
        self.version += 1
        return True

    def interrupt(self, response_id: str) -> None:
        self.responses[response_id].status = "interrupted"
        self.epoch += 1
        self.version += 1

    def context_history(self) -> list[tuple[str, str]]:
        # 教学简化：真实系统应按 turn_id 配对并按 seq 排序。
        result = [("user", text) for text in self.user_finals]
        result.extend(
            ("assistant", response.delivered_text)
            for response in self.responses.values()
            if response.delivered_text
        )
        return result


def main() -> None:
    # ---- 1. partial 不进正式历史 ----
    session = Session()
    session.update_partial("明天")
    session.update_partial("明天会下雨吗")
    session.commit_final("明天会下雨吗？")
    assert session.user_finals == ["明天会下雨吗？"]
    assert session.asr_partial == ""

    # ---- 2. 正常播放确认：第一段送达 ----
    reply = session.start_response("r1", "明天有雨，出门记得带伞。下午活动改到三点。")
    assert session.acknowledge_segment("r1", reply.epoch, "明天有雨。") is True

    # ---- 3. 打断：epoch +1，回复翻成 interrupted ----
    session.interrupt("r1")

    # ---- 4. 旧代次迟到回调：epoch 不一致，必须被拒绝 ----
    assert session.acknowledge_segment("r1", reply.epoch, "出门记得带伞。") is False

    # ---- 5. 当场改动题：迟到回调改传 session.epoch（当前代次）----
    # epoch 校验通过了，但仍因 r1.status != "playing" 被拒绝——
    # 证明「同代内打断后的追认拦截」只能靠 status，epoch 防不住这一条。
    assert session.epoch != reply.epoch, "打断必须已推进代次"
    assert session.acknowledge_segment("r1", session.epoch, "出门记得带伞。") is False, (
        "只校验 epoch 时同代迟到的 ack 会被追认，status 必须同时校验"
    )

    # ---- 6. 上下文只含已送达文本：未播完的"出门记得带伞。下午活动改到三点。"不进历史 ----
    assert session.user_finals == ["明天会下雨吗？"]
    assert session.responses["r1"].delivered_text == "明天有雨。"
    assert ("assistant", "出门记得带伞。") not in session.context_history()
    assert ("assistant", reply.generated_text) not in session.context_history()

    # ---- 7. 新代次的新回复正常工作：epoch 已 +1，新回复挂在当前代次 ----
    reply2 = session.start_response("r2", "好的，您想听越剧还是京剧？")
    assert reply2.epoch == session.epoch
    assert session.acknowledge_segment("r2", reply2.epoch, "好的。") is True
    assert session.responses["r2"].delivered_text == "好的。"

    print("PASS: partial 未污染历史，旧回复未覆盖新状态，上下文只含已送达文本")
    print(
        "PASS: 当场改动题——迟到回调传 session.epoch 后 epoch 校验通过，"
        "仍因 status != playing 被拒；生产必须同时校验 epoch（跨代淘汰）与 status（同代追认拦截）"
    )


if __name__ == "__main__":
    main()
