"""Exercise browser wire protocol using real local ONNX models and a mock ASR."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import wave
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve


class BrowserProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_enrollment_and_streaming(self):
        async def upstream(ws):
            await ws.recv()
            async for msg in ws:
                if isinstance(msg, str):
                    await ws.send(
                        json.dumps(
                            {
                                "mode": "2pass-offline",
                                "text": "网页链路正常",
                                "is_final": True,
                            }
                        )
                    )
                    return

        async with serve(upstream, "127.0.0.1", 0) as server:
            with tempfile.TemporaryDirectory() as folder:
                env = dict(
                    os.environ,
                    VOICE_DATA_DIR=folder,
                    FUNASR_URL=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                )
                process = subprocess.Popen(  # noqa: ASYNC220
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "asr.webapp:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "18765",
                    ],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    for _ in range(50):
                        try:
                            with urllib.request.urlopen(  # noqa: ASYNC210
                                "http://127.0.0.1:18765/api/status", timeout=0.3
                            ) as r:
                                self.assertFalse(json.load(r)["enrolled"])
                            break
                        except OSError:
                            await asyncio.sleep(0.1)
                    else:
                        self.fail("Web backend failed to start")
                    url = "ws://127.0.0.1:18765/ws/audio"
                    with self.assertRaises(Exception):  # noqa: B017
                        async with connect(url, origin="https://other.example"):
                            pass
                    f = (
                        Path(__file__).resolve().parents[1]
                        / "examples/speaker1_a_cn_16k.wav"
                    )
                    with wave.open(str(f)) as w:
                        pcm = w.readframes(w.getnframes())
                    async with connect(url, origin="http://127.0.0.1:8765") as ws:
                        await ws.send(
                            json.dumps({"action": "enroll", "name": "test owner"})
                        )
                        self.assertEqual(json.loads(await ws.recv())["type"], "ready")
                        for i in range(0, len(pcm), 1920):
                            await ws.send(pcm[i : i + 1920])
                        await ws.send(json.dumps({"action": "stop"}))
                        self.assertEqual(
                            json.loads(await ws.recv())["type"], "enrolled"
                        )
                        self.assertEqual(json.loads(await ws.recv())["type"], "done")
                    async with connect(url, origin="http://127.0.0.1:8765") as ws:
                        await ws.send(
                            json.dumps({"action": "listen", "endpointing": "client"})
                        )
                        self.assertEqual(json.loads(await ws.recv())["type"], "ready")

                        async def send_audio():
                            data = pcm + bytes(16000 * 2 * 2)
                            for i in range(0, len(data), 1920):
                                await ws.send(data[i : i + 1920])
                                await asyncio.sleep(0.06)
                            await ws.send(json.dumps({"action": "stop"}))

                        sender = asyncio.create_task(send_audio())
                        events = []
                        try:
                            async for raw in ws:
                                event = json.loads(raw)
                                events.append(event)
                                if event["type"] == "done":
                                    break
                        finally:
                            sender.cancel()
                            await asyncio.gather(sender, return_exceptions=True)
                        self.assertTrue(any(e.get("accepted") for e in events))
                        self.assertTrue(
                            any(e.get("text") == "网页链路正常" for e in events)
                        )
                        token_event = next(e for e in events if e.get("token"))
                        token = token_event["token"]

                    # A later utterance reuses the opaque authorization and
                    # therefore does not compute another speaker embedding.
                    async with connect(url, origin="http://127.0.0.1:8765") as ws:
                        await ws.send(
                            json.dumps(
                                {
                                    "action": "listen",
                                    "endpointing": "client",
                                    "token": token,
                                }
                            )
                        )
                        self.assertEqual(json.loads(await ws.recv())["type"], "ready")
                        for i in range(0, min(len(pcm), 19200), 1920):
                            await ws.send(pcm[i : i + 1920])
                        await ws.send(json.dumps({"action": "stop"}))
                        cached_events = []
                        async for raw in ws:
                            event = json.loads(raw)
                            cached_events.append(event)
                            if event["type"] == "done":
                                break
                        cached = next(
                            (e for e in cached_events if e["type"] == "verification"),
                            None,
                        )
                        self.assertIsNotNone(cached, cached_events)
                        self.assertTrue(cached["accepted"])
                        self.assertTrue(cached["cached"])

                    request = urllib.request.Request(
                        "http://127.0.0.1:18765/api/session/end",
                        data=json.dumps({"token": token}).encode(),
                        headers={
                            "content-type": "application/json",
                            "origin": "http://127.0.0.1:8765",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(request) as response:  # noqa: ASYNC210
                        self.assertTrue(json.load(response)["revoked"])
                finally:
                    process.terminate()
                    await asyncio.to_thread(process.wait, timeout=10)


if __name__ == "__main__":
    unittest.main()
