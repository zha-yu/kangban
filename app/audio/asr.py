"""ASR 适配层：协议契约 + 实现（Mock / FunASR 本地基线 / 云 API 骨架）。

Day 3 任务卡 A1 · 计划 `docs/plans/task-a1-ASR与TTS适配层.md` · DAG `docs/plans/task-a1-DAG.md`
契约来源：`docs/team_tasks_day03/contracts.md` §C1（冻结人 A1，消费方 A2/A3/A5）。

本文件顶部为 **C1 契约区**：逐字对齐 contracts.md，下游只准 import 这里的类型，
不得引用下列实现里的任何私有细节。改契约必须先群通告 + 计划 §10 留痕。

与 contracts.md 的机械差异（不改语义，仅为可运行与过 ruff 门禁，已在此留痕）：
1. 契约块里的 ``from typing import Literal`` 在本文件未使用，按 ruff F401 移除；
2. ``AsyncIterator`` 从 ``collections.abc`` 显式导入，供实现侧标注使用；
3. 注解按 UP037 去掉引号、按 ruff format 折行归一（Python 3.12 + future annotations 下语义等价）；
4. ``ASREventKind(str, Enum)`` 保留契约原样，加 ``noqa: UP042`` 而非改成 ``StrEnum``（冻结件不擅自换基类）。
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Protocol, cast

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.audio._observe import record_asr, start
from app.core.config import Settings, get_settings

# ---------------------------------------------------------------------------
# C1 契约区（原样拷贝自 docs/team_tasks_day03/contracts.md §C1，勿改语义）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ASRResult:
    """一次语音识别的结果。"""

    text: str  # 转写文本；partial 为中间态文本，final 为终稿
    is_final: bool  # True=终稿（句级），False=中间结果
    # 置信度：引擎提供时取 0.0~1.0；FunASR 协议层拿不到 → 恒为 None，不造 0.0 假值。
    # 纯观测指标（进 trace），不参与任何业务决策；云 API 接入后直接透传。
    confidence: float | None


class ASREventKind(str, Enum):  # noqa: UP042 - 契约冻结于 contracts.md §C1，不擅自改 StrEnum
    PARTIAL = "partial"  # 中间结果（可驱动字幕预显，业务不依赖）
    FINAL = "final"  # 终稿：路由层/业务处理的唯一触发事件
    ERROR = "error"  # 携带 ASRError 子类，触发降级链路


@dataclass(frozen=True, slots=True)
class ASREvent:
    kind: ASREventKind
    result: ASRResult | None  # ERROR 时为 None
    error: ASRError | None  # 非 ERROR 时为 None


class ASRError(Exception):
    """ASR 错误基类。message 面向日志，不直接透出给长者。"""


class ASRUnavailable(ASRError):
    """服务不可达/超时/容器未启动 → 可退避重试（≤2 次，指数退避），重试耗尽走降级话术。"""


class ASRProtocolError(ASRError):
    """协议错帧/握手失败/响应非法 → 不可重试，直接降级话术并打 trace 标记。"""


class ASRProvider:
    """ASR 适配器协议。实现：FunASRAdapter / MockASRAdapter / CloudASRAdapter(骨架)。"""

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        """输入 16kHz/16bit/单声道 60ms PCM 帧流，输出事件流。
        约束：不阻塞事件循环；连接级失败抛 ASRError 子类；identity 由调用方 runtime_context 注入，
        本协议不接受任何客户端传 elder_id。"""
        ...


class TTSProvider:
    """TTS 适配器协议。实现：EdgeTTSAdapter(整段合成，非流式) / CloudTTSAdapter(骨架)。"""

    async def synthesize(self, text: str) -> bytes:
        """输出 16kHz/16bit/单声道 PCM 整段音频（Day 3 非流式口径，首包预算受限已登记）。"""
        ...


# ---------------------------------------------------------------------------
# 实现区（T1.1 / T1.2 由 asr-core 填充）
# 约定：MockASRProvider / FunASRWSProvider / CloudASRProvider + build_asr()
# ---------------------------------------------------------------------------

logger = logging.getLogger("app.audio.asr")

# ------------------------------ 音频口径（C1 钉死） ------------------------------
PCM_SAMPLE_RATE: Final[int] = 16_000
PCM_SAMPLE_WIDTH: Final[int] = 2  # 16bit
PCM_CHANNELS: Final[int] = 1  # 单声道
FRAME_INTERVAL_MS: Final[int] = 60
# 60ms × 16kHz × 2B × 1ch = 1920B。切帧归 A3，本文件只登记口径、不重切。
FRAME_BYTES: Final[int] = (
    PCM_SAMPLE_RATE * FRAME_INTERVAL_MS // 1000 * PCM_SAMPLE_WIDTH * PCM_CHANNELS
)

# ---------------------------- FunASR 2pass 协议口径 ----------------------------
FUNASR_DEFAULT_URL: Final[str] = "ws://localhost:7002"  # 7001 已给 laya，见计划 §9-1
FUNASR_HANDSHAKE: Final[dict[str, object]] = {
    "mode": "2pass",
    "chunk_size": [5, 10, 5],
    "audio_fs": PCM_SAMPLE_RATE,
    "wav_format": "pcm",
    "itn": True,
}
# 收完音频后等 offline 终稿的总预算：对端不回必须自己收口，否则调用方会一直挂着。
FUNASR_RECEIVE_TIMEOUT_S: Final[float] = 15.0
FUNASR_CONNECT_TIMEOUT_S: Final[float] = 5.0
FUNASR_END_OF_SPEECH: Final[dict[str, bool]] = {"is_speaking": False}
FUNASR_END_OF_STREAM: Final[dict[str, bool]] = {"is_end": True}

# Mock 脚本夹具：app/ 不反向 import tests/（AGENTS §2 分层单向），只按固定路径读文件。
_MOCK_SCRIPT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "asr_script.py"
)
DEFAULT_MOCK_SCRIPT: Final[str] = "ASR_MOCK_SCRIPT"


# ------------------------------- Mock 适配器 -------------------------------
@dataclass(frozen=True, slots=True)
class MockASRStep:
    """Mock 脚本的一步：`is_final=True` 的那步是终稿，之后不再有事件。"""

    text: str
    is_final: bool


def _constant_value(tree: ast.Module, constant: str) -> object:
    """在 AST 里取模块级常量值（`X = ...` 与 `X: T = ...` 两种写法都认）。"""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == constant:
                return ast.literal_eval(value)
    raise ValueError(f"{_MOCK_SCRIPT_PATH.name} 缺少常量 {constant!r}")


def _steps_from(value: object) -> list[MockASRStep]:
    """把字面量校验成步骤列表：坏夹具在读取时就炸，别拖到运行时。"""
    if not isinstance(value, list) or not value:
        raise ValueError("Mock 脚本必须是非空 list")
    steps: list[MockASRStep] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"Mock 脚本第 {index} 项必须是 dict")
        text = item.get("text")
        is_final = item.get("is_final")
        if not isinstance(text, str) or not isinstance(is_final, bool):
            raise ValueError(f"Mock 脚本第 {index} 项需要 text: str + is_final: bool")
        steps.append(MockASRStep(text=text, is_final=is_final))
    if not any(step.is_final for step in steps):
        raise ValueError("Mock 脚本至少要有一步 is_final=True，否则适配器不会收口")
    return steps


def load_mock_script(
    constant: str = DEFAULT_MOCK_SCRIPT, path: Path | None = None
) -> list[MockASRStep]:
    """读固定脚本文件里的指定常量，返回强类型步骤列表。"""
    source = path if path is not None else _MOCK_SCRIPT_PATH
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    return _steps_from(_constant_value(tree, constant))


async def _drain_audio(audio: AsyncIterator[bytes]) -> None:
    """把没人消费的音频迭代器读到尾：生产者才不会卡在 yield 上。"""
    with contextlib.suppress(Exception):
        async for _ in audio:
            pass


class MockASRProvider(ASRProvider):
    """离线假 ASR：按固定脚本吐 PARTIAL×N → FINAL，不发一次网络请求。

    用途是 A2/A3/A5 在没有教师 key、没有 FunASR 时也能起全链路（计划 §T1.1）。
    `latency_ms` 模拟逐包间隔，`raise_error` 模拟故障降级，两个旋钮都只影响本类。
    """

    name = "mock"

    def __init__(
        self,
        *,
        script: Sequence[MockASRStep] | None = None,
        latency_ms: float = 0.0,
        raise_error: ASRError | None = None,
    ) -> None:
        self._script = list(script) if script is not None else None
        self._latency_ms = latency_ms
        self._raise_error = raise_error
        self._drain_task: asyncio.Task[None] | None = None

    @property
    def script(self) -> list[MockASRStep]:
        """首次访问才读夹具文件：导入本模块不该产生文件 I/O。"""
        if self._script is None:
            self._script = load_mock_script()
        return self._script

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        if self._raise_error is not None:
            raise self._raise_error
        return self._events(audio)

    async def _events(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        watch = start()
        first_packet_ms: float | None = None
        last_text_len = 0
        last_is_final = False
        self._drain_task = asyncio.create_task(_drain_audio(audio))
        try:
            for step in self.script:
                if self._latency_ms > 0:
                    await asyncio.sleep(self._latency_ms / 1000)
                if first_packet_ms is None:
                    first_packet_ms = watch.elapsed_ms()
                last_text_len = len(step.text)
                last_is_final = step.is_final
                kind = ASREventKind.FINAL if step.is_final else ASREventKind.PARTIAL
                yield ASREvent(
                    kind=kind,
                    result=ASRResult(text=step.text, is_final=step.is_final, confidence=None),
                    error=None,
                )
                if step.is_final:
                    return
        finally:
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_task
            record_asr(
                provider=self.name,
                source="mock",
                text_len=last_text_len,
                is_final=last_is_final,
                confidence=None,
                first_packet_ms=first_packet_ms,
                total_ms=watch.elapsed_ms(),
            )


# ------------------------------ FunASR 适配器 ------------------------------
class _WebSocket(Protocol):
    """本适配器用到的连接能力（只声明用到的三个方法）。

    不直接 import `websockets` 的连接类型：既避免把某个 websockets 版本的类钉进入口签名
    （测试里可以塞假连接），也让 mypy 不必依赖第三方 stub。
    """

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    async def send(self, message: str | bytes) -> None: ...

    async def close(self) -> None: ...


async def _safe_close(connection: _WebSocket) -> None:
    """关连接并吞掉关闭期异常：对端可能已经先断，这里再报一次没有意义。"""
    with contextlib.suppress(OSError, WebSocketException):
        await connection.close()


def _error_event(error: ASRError) -> ASREvent:
    return ASREvent(kind=ASREventKind.ERROR, result=None, error=error)


def _parse_funasr_frame(raw: str | bytes) -> ASREvent:
    """把一帧 FunASR 结果解析成事件（online → PARTIAL / offline → FINAL）。

    `confidence` 恒为 `None`：FunASR 协议层没有该字段，禁止填 0.0 冒充「零置信」。
    文本原样返回（不 strip），空终稿交给 A2/A5 按 PRD FR-UI-05 说「没听清」。
    """

    def fail(reason: str) -> ASRProtocolError:
        # 只报结构与长度，不回显对端原文：原文是长者语音转写，属敏感内容。
        return ASRProtocolError(f"FunASR 回帧非法：{reason}（原始长度 {len(raw)}）")

    if not isinstance(raw, str):
        return _error_event(fail("收到二进制帧"))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _error_event(fail("不是合法 JSON"))
    if not isinstance(payload, dict):
        return _error_event(fail("JSON 顶层不是对象"))
    mode = payload.get("mode")
    text = payload.get("text")
    if not isinstance(mode, str) or not isinstance(text, str):
        return _error_event(fail("缺少 mode/text 字段"))
    if "online" in mode:
        is_final = payload.get("is_final") is True
    elif "offline" in mode:
        is_final = True
    else:
        return _error_event(fail(f"未知 mode={mode!r}"))
    kind = ASREventKind.FINAL if is_final else ASREventKind.PARTIAL
    return ASREvent(
        kind=kind,
        result=ASRResult(text=text, is_final=is_final, confidence=None),
        error=None,
    )


class FunASRWSProvider(ASRProvider):
    """FunASR 2pass 流式 WebSocket 适配器（本地基线；云端为主实现，见计划 P-A1-6）。

    四条协议口径（计划 §T1.2，改动要过主理人）：

    1. 握手帧固定 `FUNASR_HANDSHAKE`，`subprotocols=["binary"]` 声明音频走二进制帧；
    2. `ping_interval=None` —— FunASR 对 ping 帧会判超时并踢连接，**禁止**开；
    3. 音频帧原样发（60ms/1920B 由 A3 切好），收尾 `is_speaking=false` + `is_end=true`；
    4. `asyncio.timeout(15)` 包住整个接收循环等 offline 终稿，对端不回就自己收口。

    错误二分类（C1）：建连失败/超时/对端断开 → `ASRUnavailable`（可重试 ≤2 次退避）；
    非法 JSON/缺字段/未知 mode → `ASRProtocolError`（不可重试）。建连阶段直接抛，
    流中阶段以 `ERROR` 事件产出 —— C1 的 `ASREvent.error` 就是留给 A2 映射降级帧的。

    调用约定（C1 的 `stream` 是 async def 返回迭代器）：
        events = await provider.stream(frames)
        async for event in events: ...
    消费方请把迭代器读到自然结束：FINAL / ERROR 一定落在最后一个事件，连接关闭与观测埋点
    挂在生成器收尾上；用 `break` 提前跳出会把清理推迟到 GC（实现方刻意不持有全局引用）。
    """

    name = "funasr"

    def __init__(
        self,
        *,
        url: str = FUNASR_DEFAULT_URL,
        receive_timeout: float = FUNASR_RECEIVE_TIMEOUT_S,
        connect_timeout: float = FUNASR_CONNECT_TIMEOUT_S,
    ) -> None:
        self._url = url
        self._receive_timeout = receive_timeout
        self._connect_timeout = connect_timeout

    @property
    def url(self) -> str:
        return self._url

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        connection = await self._connect()
        try:
            # 先握手再返回生成器：连不上/握手失败必须在 `await stream()` 这层就炸。
            await connection.send(json.dumps(FUNASR_HANDSHAKE))
        except (ConnectionClosed, WebSocketException) as exc:
            await _safe_close(connection)
            raise ASRUnavailable(f"FunASR 握手帧发送失败：{exc}") from exc
        return self._events(connection, audio)

    async def _connect(self) -> _WebSocket:
        try:
            connection = await websockets.connect(
                self._url,
                subprotocols=["binary"],
                ping_interval=None,  # 禁 ping 帧：开了会被 FunASR 判超时踢掉
                open_timeout=self._connect_timeout,
            )
        except (OSError, WebSocketException) as exc:  # TimeoutError 是 OSError 子类
            raise ASRUnavailable(f"FunASR 连接失败：{exc}") from exc
        return cast("_WebSocket", connection)

    async def _events(
        self, connection: _WebSocket, audio: AsyncIterator[bytes]
    ) -> AsyncIterator[ASREvent]:
        events: asyncio.Queue[ASREvent | None] = asyncio.Queue()
        watch = start()
        first_packet_ms: float | None = None
        last: ASREvent = _error_event(ASRUnavailable("FunASR 未产出任何结果"))
        receiver = asyncio.create_task(self._receive(connection, events))
        sender = asyncio.create_task(self._send_audio(connection, audio))
        try:
            while (event := await events.get()) is not None:
                if first_packet_ms is None:
                    first_packet_ms = watch.elapsed_ms()
                last = event
                yield event
                if event.kind is not ASREventKind.PARTIAL:
                    # FINAL 与 ERROR 都是终态：ERROR 之后连接已经不可信，别再读第二段故障
                    return
        finally:
            for task in (receiver, sender):
                task.cancel()
            await asyncio.gather(receiver, sender, return_exceptions=True)
            await _safe_close(connection)
            record_asr(
                provider=self.name,
                source="funasr",
                text_len=len(last.result.text) if last.result is not None else 0,
                is_final=last.result.is_final if last.result is not None else False,
                confidence=None,
                first_packet_ms=first_packet_ms,
                total_ms=watch.elapsed_ms(),
            )

    async def _receive(
        self, connection: _WebSocket, events: asyncio.Queue[ASREvent | None]
    ) -> None:
        # 计时器挂在这条 task 里，而不是消费侧生成器里：生成器一 yield 就挂起，
        # 挂在它身上的 timeout 只会在调用方下一次 await 才炸，炸点会串到别人的帧上。
        try:
            async with asyncio.timeout(self._receive_timeout):
                async for raw in connection:
                    event = _parse_funasr_frame(raw)
                    await events.put(event)
                    if event.kind is not ASREventKind.PARTIAL:
                        return
                raise ASRUnavailable("FunASR 在终稿前关闭了连接")
        except TimeoutError:
            await events.put(
                _error_event(ASRUnavailable(f"FunASR {self._receive_timeout:.1f}s 内没有等到终稿"))
            )
        except (ConnectionClosed, WebSocketException) as exc:
            await events.put(_error_event(ASRUnavailable(f"FunASR 连接异常断开：{exc}")))
        except ASRError as exc:
            await events.put(_error_event(exc))
        finally:
            events.put_nowait(None)

    async def _send_audio(self, connection: _WebSocket, audio: AsyncIterator[bytes]) -> None:
        async for chunk in audio:
            await connection.send(chunk)
        await connection.send(json.dumps(FUNASR_END_OF_SPEECH))
        await connection.send(json.dumps(FUNASR_END_OF_STREAM))


class CloudASRProvider(ASRProvider):
    """云端流式 ASR 占位（等教师统一 key，计划 §8 P-A1-2）。

    与 `CloudTTSProvider` 的 `NotImplementedError` 有意不对称：ASR 的 C1 降级信号是
    `ASRUnavailable`，上游（A2）按可重试故障走「整段上传 → 输入框」链路，不该看到编程错误。
    """

    name = "cloud"

    def __init__(
        self,
        *,
        base_url: str = "",
        app_id: str = "",
        api_key: str = "",
        timeout_ms: int = 300,
    ) -> None:
        self._base_url = base_url
        self._app_id = app_id
        self._api_key = api_key
        self._timeout_ms = timeout_ms

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[ASREvent]:
        raise ASRUnavailable("云端 ASR 尚未接入（等教师统一 key，见计划 §8 P-A1-2）")


# --------------------------------- 工厂 ---------------------------------
def build_asr(settings: Settings | None = None) -> ASRProvider:
    """按配置装配 ASR 适配器（唯一入口）。

    归一规则：`ASR_PROVIDER` 为空或未知 → `mock`（断网可跑，符合 PRD 的 Mock 边界）；
    取值大小写与首尾空白不敏感。配置只读，不回写 `settings`。
    """
    config = settings if settings is not None else get_settings()
    kind = (config.asr_provider or "").strip().lower()
    if kind == "funasr":
        return FunASRWSProvider(url=config.asr_base_url.strip() or FUNASR_DEFAULT_URL)
    if kind == "cloud":
        return CloudASRProvider(
            base_url=config.asr_base_url.strip(),
            app_id=config.asr_app_id,
            api_key=config.asr_api_key,
            timeout_ms=config.asr_timeout_ms,
        )
    if kind not in ("", "mock"):
        logger.warning("unknown ASR_PROVIDER=%r, fallback to mock", kind)
    return MockASRProvider()
