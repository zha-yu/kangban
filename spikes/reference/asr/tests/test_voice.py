import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from websockets.asyncio.server import serve

from asr.voice import RATE, Config, SpeakerGate


class Vad:
    def accept_waveform(self, block):
        self.active = bool(np.any(block))

    def is_speech_detected(self):
        return self.active


class Gate(SpeakerGate):
    def __init__(self, url, match=True):
        self.config = Config(url=url)
        self.fingerprint = "test"
        self.extractor = type("Extractor", (), {"dim": 2})()
        self.match = match

    def vad(self):
        return Vad()

    def embedding(self, audio):
        return np.array([1.0, 0.0] if self.match else [0.0, 1.0])


async def source():
    for _ in range(40):
        yield np.ones(960, dtype=np.float32) * 0.1
    for _ in range(20):
        yield np.zeros(960, dtype=np.float32)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_stream_and_final(self):
        frames = []

        async def server(ws):
            start = json.loads(await ws.recv())
            self.assertEqual(start["audio_fs"], RATE)
            self.assertEqual(start["mode"], "2pass")
            async for msg in ws:
                if isinstance(msg, bytes):
                    frames.append(msg)
                    if len(frames) == 1:
                        await ws.send(
                            json.dumps(
                                {
                                    "mode": "2pass-online",
                                    "text": "你好",
                                    "is_final": False,
                                }
                            )
                        )
                else:
                    self.assertFalse(json.loads(msg)["is_speaking"])
                    await ws.send(
                        json.dumps(
                            {
                                "mode": "2pass-offline",
                                "text": "你好。",
                                "is_final": True,
                            }
                        )
                    )
                    break

        async with serve(server, "127.0.0.1", 0) as host:
            gate = Gate(f"ws://127.0.0.1:{host.sockets[0].getsockname()[1]}")
            events = await self.run_gate(gate)
        self.assertGreater(len(frames), 40)  # includes verification prefix
        self.assertEqual(events[-1]["state"], "idle")
        self.assertTrue(any(e.get("text") == "你好。" for e in events))

    async def run_gate(self, gate):
        events = []

        async def emit(e):
            events.append(e)

        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "speaker.json"
            p.write_text(
                json.dumps(
                    {"name": "owner", "model_sha256": "test", "embedding": [1, 0]}
                )
            )
            await gate.run(p, source(), emit)
        return events

    async def test_rejected_never_connects(self):
        with patch("asr.voice.connect", side_effect=AssertionError("must not connect")):
            events = await self.run_gate(Gate("ws://unused", match=False))
        self.assertFalse(
            next(e for e in events if e["type"] == "verification")["accepted"]
        )

    async def test_client_endpoint_preserves_pause_and_all_audio(self):
        received = []

        async def server(ws):
            await ws.recv()
            async for msg in ws:
                if isinstance(msg, bytes):
                    received.append(msg)
                else:
                    await ws.send(json.dumps({"is_final": True}))
                    break

        async def audio():
            for value, count in [(0.1, 40), (0, 30), (0.2, 10)]:
                for _ in range(count):
                    yield np.full(960, value, dtype=np.float32)

        async with serve(server, "127.0.0.1", 0) as host:
            gate = Gate(f"ws://127.0.0.1:{host.sockets[0].getsockname()[1]}")
            with tempfile.TemporaryDirectory() as folder:
                p = Path(folder) / "speaker.json"
                p.write_text(
                    json.dumps(
                        {"name": "owner", "model_sha256": "test", "embedding": [1, 0]}
                    )
                )

                async def emit(e):
                    pass

                await gate.run(p, audio(), emit, endpointing="client")
        self.assertEqual(len(received), 80)
        self.assertTrue(
            all(np.frombuffer(frame, "<i2")[0] == 3276 for frame in received[:40])
        )
        self.assertTrue(
            all(np.frombuffer(frame, "<i2")[0] == 0 for frame in received[40:70])
        )
        self.assertTrue(
            all(np.frombuffer(frame, "<i2")[0] == 6553 for frame in received[70:])
        )

    async def test_final_timeout(self):
        async def server(ws):
            async for msg in ws:
                if isinstance(msg, str) and json.loads(msg).get("is_end"):
                    await asyncio.sleep(0.2)
                    break

        async with serve(server, "127.0.0.1", 0) as host:
            gate = Gate(f"ws://127.0.0.1:{host.sockets[0].getsockname()[1]}")
            gate.config.result_timeout = 0.05
            with self.assertRaises(TimeoutError):
                await self.run_gate(gate)

    async def test_server_authorization_skips_embedding_and_streams_every_frame(self):
        received = []

        async def server(ws):
            await ws.recv()
            async for msg in ws:
                if isinstance(msg, bytes):
                    received.append(msg)
                else:
                    await ws.send(json.dumps({"is_final": True}))
                    break

        async with serve(server, "127.0.0.1", 0) as host:
            gate = Gate(f"ws://127.0.0.1:{host.sockets[0].getsockname()[1]}")
            gate.embedding = lambda _: (_ for _ in ()).throw(
                AssertionError("authorized sessions must not compute an embedding")
            )
            events = []

            async def emit(event):
                events.append(event)

            with tempfile.TemporaryDirectory() as folder:
                profile = Path(folder) / "speaker.json"
                profile.write_text(
                    json.dumps(
                        {"name": "owner", "model_sha256": "test", "embedding": [1, 0]}
                    )
                )
                await gate.run(
                    profile, source(), emit, endpointing="client", authorized=True
                )

        self.assertEqual(len(received), 60)
        verified = next(e for e in events if e["type"] == "verification")
        self.assertTrue(verified["accepted"])
        self.assertTrue(verified["cached"])


if __name__ == "__main__":
    unittest.main()
