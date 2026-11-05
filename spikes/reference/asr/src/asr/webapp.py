"""Local browser gateway for speaker enrollment and wake sessions."""

import asyncio
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .providers.asr import FunASRWSProvider
from .providers.orchestrator import VoiceSessionOrchestrator
from .providers.voiceprint import SherpaVoiceprintProvider

RATE = 16000
MODEL_DIR = (
    Path(__file__).resolve().parents[2] / "funasr-runtime-resources/models/sherpa-onnx"
)
MODEL_DIR = Path(os.environ.get("MODEL_DIR", MODEL_DIR))

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("VOICE_DATA_DIR", str(ROOT.parents[1] / "data")))
PROFILE = DATA / "speaker.json"
app = FastAPI()
# Static browser-only VAD demo. These mounts serve assets, never audio uploads.
app.mount(
    "/vad-assets",
    StaticFiles(directory=MODEL_DIR / "sherpa-onnx-wasm-simd-v2.0.1-ten-vad"),
    name="vad-assets",
)


@app.get("/vad/sample.wav")
async def vad_sample():
    return FileResponse(
        ROOT.parents[1] / "examples/speaker1_a_cn_16k.wav", media_type="audio/wav"
    )


app.mount("/vad", StaticFiles(directory=ROOT / "web/vad", html=True), name="vad-demo")


class SessionTokens:
    """Short-lived, opaque authorizations bound to one speaker profile."""

    def __init__(self, ttl_seconds=300):
        self.ttl_seconds = ttl_seconds
        self._items = {}

    def issue(self, profile_revision):
        now = time.time()
        token = secrets.token_urlsafe(32)
        expires_at = now + self.ttl_seconds
        self._items[token] = (expires_at, profile_revision)
        self.prune(now)
        return token, expires_at

    def validate(self, token, profile_revision):
        now = time.time()
        item = self._items.get(token)
        if (
            item is None
            or item[0] <= now
            or not secrets.compare_digest(item[1], profile_revision)
        ):
            self._items.pop(token, None)
            return None
        return item[0]

    def revoke(self, token):
        return self._items.pop(token, None) is not None

    def clear(self):
        self._items.clear()

    def prune(self, now=None):
        now = time.time() if now is None else now
        for token, (expires_at, _) in list(self._items.items()):
            if expires_at <= now:
                self._items.pop(token, None)


token_ttl = max(30, min(3600, int(os.environ.get("VOICE_TOKEN_TTL", "300"))))
tokens = SessionTokens(token_ttl)


def profile_revision():
    return hashlib.sha256(PROFILE.read_bytes()).hexdigest()


def allowed_origin(origin):
    return origin in (
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "https://127.0.0.1:8765",
    )


def create_orchestrator(threshold: float = 0.55) -> VoiceSessionOrchestrator:
    """创建 Provider 编排器（替代原 SpeakerGate）"""
    voiceprint = SherpaVoiceprintProvider(model_dir=MODEL_DIR, threshold=threshold)
    asr = FunASRWSProvider(
        url=os.environ.get("FUNASR_URL", "wss://127.0.0.1:10096"),
        insecure=os.environ.get("FUNASR_INSECURE", "0") == "1",
    )
    return VoiceSessionOrchestrator(voiceprint, asr, PROFILE)


@app.get("/")
async def index():
    return FileResponse(ROOT / "web/index.html")


@app.get("/app.js")
async def browser_app():
    return FileResponse(ROOT / "web/app.js", media_type="text/javascript")


@app.get("/audio-worklet.js")
async def worklet():
    return FileResponse(ROOT / "web/audio-worklet.js", media_type="text/javascript")


@app.get("/api/status")
async def status():
    name = None
    if PROFILE.exists():
        name = json.loads(PROFILE.read_text(encoding="utf-8"))["name"]
    return {"enrolled": name is not None, "name": name, "token_ttl": token_ttl}


@app.post("/api/session/end")
async def end_session(request: Request):
    if not allowed_origin(request.headers.get("origin")):
        return {"revoked": False}
    body = await request.json()
    token = body.get("token")
    return {"revoked": bool(token and tokens.revoke(str(token)))}


@app.websocket("/ws/audio")
async def session(ws: WebSocket):
    # Browser access stays local, including protection against cross-site enrollment.
    if not allowed_origin(ws.headers.get("origin")):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        init = await asyncio.wait_for(ws.receive_json(), 5)
        action = init.get("action")
        if action not in ("enroll", "listen"):
            raise ValueError("无效的操作")
        threshold = float(init.get("threshold", 0.55))
        if not 0 <= threshold <= 1:
            raise ValueError("声纹阈值必须在 0 到 1 之间")
        endpointing = init.get("endpointing", "server")
        if endpointing not in ("client", "server"):
            raise ValueError("无效的端点检测模式")
        orchestrator = create_orchestrator(threshold=threshold)
        supplied_token = str(init.get("token") or "")
        skip_verify = bool(init.get("skip_verify"))
        authorized = False
        expires_at = None
        if action == "listen" and supplied_token and PROFILE.exists():
            expires_at = tokens.validate(supplied_token, profile_revision())
            authorized = expires_at is not None
        if (
            action == "listen"
            and not skip_verify
            and not authorized
            and not PROFILE.exists()
        ):
            raise ValueError("请先录入目标用户声纹，或开启声纹认证开关的「跳过」模式")
        await ws.send_json({"type": "ready"})

        async def blocks():
            import numpy as np

            async with asyncio.timeout(45):
                while True:
                    message = await asyncio.wait_for(ws.receive(), 10)
                    if message["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect()
                    data = message.get("bytes")
                    if data is not None:
                        if not data or len(data) % 2 or len(data) > RATE * 2:
                            raise ValueError("音频分片格式错误")
                        yield np.frombuffer(data, "<i2").astype(np.float32) / 32768
                    elif (
                        json.loads(message.get("text") or "{}").get("action") == "stop"
                    ):
                        break
                    else:
                        raise ValueError("无效的音频消息")

        if action == "enroll":
            name = str(init.get("name", "")).strip()
            if not name or len(name) > 40:
                raise ValueError("请填写 1–40 字的用户名称")
            chunks, samples = [], 0
            async for block in blocks():
                samples += len(block)
                if samples > RATE * 15:
                    raise ValueError("录入音频不能超过 15 秒")
                chunks.append((block * 32767).astype("<i2").tobytes())

            # 用 Provider 录入声纹
            voiceprint = SherpaVoiceprintProvider(model_dir=MODEL_DIR)
            await voiceprint.enroll(name, chunks)
            tokens.clear()
            await ws.send_json({"type": "enrolled", "name": name})
        else:
            # profile 检查已在前面处理（skip_verify 模式可跳过）

            async def emit(event):
                nonlocal supplied_token, expires_at
                if (
                    event.get("type") == "verification"
                    and event.get("accepted")
                    and not event.get("cached")
                    and not event.get("skip_verify")
                ):
                    supplied_token, expires_at = tokens.issue(profile_revision())
                    event = dict(
                        event,
                        token=supplied_token,
                        expires_at=expires_at,
                        ttl_seconds=token_ttl,
                    )
                elif event.get("type") == "verification" and authorized:
                    event = dict(
                        event,
                        expires_at=expires_at,
                        ttl_seconds=max(0, int(expires_at - time.time())),
                    )
                await ws.send_json(event)

            await orchestrator.run(
                blocks(),
                emit,
                endpointing=endpointing,
                authorized=authorized,
                skip_verify=skip_verify,
            )
        await ws.send_json({"type": "done"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            message = str(exc) or type(exc).__name__
            await ws.send_json({"type": "error", "message": message})
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        try:
            await ws.close()
        except RuntimeError:
            pass


def main():
    import uvicorn

    uvicorn.run("asr.webapp:app", host="127.0.0.1", port=8765, ws_max_size=32768)


if __name__ == "__main__":
    main()
