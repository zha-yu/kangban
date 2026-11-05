"""模拟浏览器完整走一遍 /ws/audio 链路，观察 2pass-offline 结果"""

import asyncio
import json
import sys
import wave

from websockets.asyncio.client import connect


async def main():
    # 先检查有没有声纹档案
    from pathlib import Path

    profile = Path("data/speaker.json")
    if not profile.exists():
        print(
            "❌ 请先录入声纹：uv run python -m asr.voice enroll --name 测试 --wav examples/speaker1_a_cn_16k.wav --profile data/speaker.json"
        )
        sys.exit(1)

    ws = await connect(
        "ws://127.0.0.1:8765/ws/audio",
        additional_headers={"Origin": "http://127.0.0.1:8765"},
    )
    print("✅ 已连接 voice-web /ws/audio")

    # 发 init（listen 动作，不带 token，模拟首句）
    await ws.send(
        json.dumps({"action": "listen", "threshold": 0.55, "endpointing": "client"})
    )
    print("✅ 已发送 listen init")

    # 等 ready
    msg = await ws.recv()
    print(f"   init 响应: {msg}")

    # 发音频（用示例 WAV，60ms 一块，和前端一样）
    with wave.open("examples/speaker1_a_cn_16k.wav", "rb") as w:
        data = w.readframes(w.getnframes())

    # 先等 verification 通过再发 stop
    # 发音频块的同时收消息
    chunk = 960 * 2
    sent = 0
    verified = False
    final_texts = []
    online_texts = []

    while True:
        # 发 3 块音频（模拟实时发）
        for _ in range(3):
            i = sent
            if i >= len(data):
                break
            await ws.send(data[i : i + chunk])
            sent += chunk
        if sent >= len(data):
            break

        # 收消息（非阻塞等 0.3s）
        try:
            msg = await asyncio.wait_for(ws.recv(), 0.3)
            m = json.loads(msg)
            print(f"   → {m}")
            if m.get("type") == "verification":
                verified = m.get("accepted")
            if m.get("type") == "transcript":
                mode = m.get("mode", "")
                if "offline" in mode:
                    final_texts.append(m.get("text", ""))
                else:
                    online_texts.append(m.get("text", ""))
        except TimeoutError:
            pass

    print(f"\n已发送 {sent} 字节音频，verification={'✅' if verified else '❌'}")
    print("现在发送 stop（模拟前端 VAD 检测到静音）...")
    await ws.send(json.dumps({"action": "stop"}))

    # 收剩下的所有消息，最多等 10 秒
    print("\n=== stop 后的消息 ===")
    import time

    start = time.time()
    while time.time() - start < 15:
        try:
            msg = await asyncio.wait_for(ws.recv(), 2)
            m = json.loads(msg)
            print(f"   → {m}")
            if m.get("type") == "transcript":
                mode = m.get("mode", "")
                if "offline" in mode:
                    final_texts.append(m.get("text", ""))
                else:
                    online_texts.append(m.get("text", ""))
            if m.get("type") == "done":
                print("\n✅ 收到 done，会话正常结束")
                break
        except TimeoutError:
            print("   (等待超时，可能已无消息)")
            break

    print("\n=== 最终结果 ===")
    print(f"online ({len(online_texts)}条): {online_texts}")
    print(f"offline ({len(final_texts)}条): {final_texts}")

    if not final_texts:
        print("\n⚠️  没有收到 2pass-offline 结果！这就是看不到标点的原因！")
    else:
        print(f"\n✅ offline 结果（带标点）: {final_texts[-1]}")

    await ws.close()


asyncio.run(main())
