"""康伴数字人 WebRTC Agent：Provider 可插拔架构"""

import asyncio
import json
import logging
import ssl

import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

from .providers.kangban_agent import KangbanDigitalHuman, create_default_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AGENT_UID = "agent001"
SIGNAL_URL = "wss://127.0.0.1:8765/ws"

ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

digital_human: KangbanDigitalHuman | None = None
pc = None
is_busy = False
ws = None  # 全局 WebSocket 引用，用于主动呼叫


async def initiate_call(target_uid: str):
    """Agent 主动呼叫网页端"""
    global pc, is_busy, ws
    if is_busy:
        logger.warning("Agent 正在通话中，无法发起新呼叫")
        return False
    if not ws:
        logger.error("WebSocket 未连接")
        return False

    logger.info(f"Agent 主动呼叫 {target_uid}")
    await ws.send(
        json.dumps(
            {
                "type": "invite",
                "from": AGENT_UID,
                "to": target_uid,
            }
        )
    )
    return True


async def agent_loop():
    global digital_human, pc, is_busy, ws

    digital_human = create_default_agent()
    logger.info("数字人初始化完成")

    ws = await websockets.connect(SIGNAL_URL, ssl=ssl_context)
    await ws.send(json.dumps({"type": "login", "uid": AGENT_UID}))
    logger.info(f"Agent {AGENT_UID} 上线成功")

    async def ping_task():
        while True:
            await asyncio.sleep(12)
            try:
                await ws.send(json.dumps({"type": "ping"}))
            except:
                break

    asyncio.create_task(ping_task())

    # 命令行接口：输入 target_uid 发起主动呼叫
    async def cli_task():
        loop = asyncio.get_event_loop()
        while True:
            try:
                cmd = await loop.run_in_executor(
                    None, input, "输入 target_uid 呼叫 (或 q 退出): "
                )
                cmd = cmd.strip()
                if cmd == "q":
                    break
                if cmd:
                    await initiate_call(cmd)
            except EOFError:
                break
            except Exception as e:
                logger.error(f"CLI 错误: {e}")

    asyncio.create_task(cli_task())

    async for raw_msg in ws:
        msg = json.loads(raw_msg)
        msg_type = msg.get("type")
        sender_uid = msg.get("from")

        if msg_type == "invite":
            logger.info(f"收到来自 {sender_uid} 的呼叫邀请")
            await ws.send(
                json.dumps({"type": "accept", "from": AGENT_UID, "to": sender_uid})
            )

        elif msg_type == "accept":
            # 网页接受了 Agent 的主动呼叫，现在 Agent 创建 offer
            logger.info(f"{sender_uid} 接受了呼叫，创建 offer")
            is_busy = True
            pc = RTCPeerConnection()

            @pc.on("track")
            def on_track(track):
                logger.info(f"收到远端 track: {track.kind}")
                if track.kind == "audio" and digital_human:
                    # 直接启动 ASR/TTS 链路（主动呼叫场景）
                    asyncio.create_task(digital_human._process_asr(track))

            @pc.on("icecandidate")
            def on_icecandidate(candidate):
                if candidate:
                    asyncio.create_task(
                        ws.send(
                            json.dumps(
                                {
                                    "type": "candidate",
                                    "from": AGENT_UID,
                                    "to": sender_uid,
                                    "candidate": {
                                        "candidate": candidate.candidate,
                                        "sdpMid": candidate.sdpMid,
                                        "sdpMLineIndex": candidate.sdpMLineIndex,
                                    },
                                }
                            )
                        )
                    )

            # 添加 TTS 推送轨
            if digital_human and digital_human.tts_track:
                pc.addTrack(digital_human.tts_track)

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await ws.send(
                json.dumps(
                    {
                        "type": "offer",
                        "from": AGENT_UID,
                        "to": sender_uid,
                        "sdp": offer.sdp,
                    }
                )
            )

        elif msg_type == "offer":
            logger.info("收到 offer，建立通话")
            is_busy = True
            pc = RTCPeerConnection()

            @pc.on("track")
            def on_track(track):
                logger.info(f"收到远端 track: {track.kind}")
                asyncio.create_task(
                    digital_human.handle_call(
                        RTCSessionDescription(sdp=msg["sdp"], type=msg["type"]),
                        sender_uid,
                        ws,
                        call_token=msg.get("token"),
                    )
                )

            @pc.on("icecandidate")
            def on_icecandidate(candidate):
                if candidate:
                    asyncio.create_task(
                        ws.send(
                            json.dumps(
                                {
                                    "type": "candidate",
                                    "from": AGENT_UID,
                                    "to": sender_uid,
                                    "candidate": {
                                        "candidate": candidate.candidate,
                                        "sdpMid": candidate.sdpMid,
                                        "sdpMLineIndex": candidate.sdpMLineIndex,
                                    },
                                }
                            )
                        )
                    )

            # 添加 TTS 推送轨
            if digital_human and digital_human.tts_track:
                pc.addTrack(digital_human.tts_track)

            offer = RTCSessionDescription(sdp=msg["sdp"], type=msg["type"])
            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await ws.send(
                json.dumps(
                    {
                        "type": "answer",
                        "from": AGENT_UID,
                        "to": sender_uid,
                        "sdp": answer.sdp,
                    }
                )
            )

        elif msg_type == "answer":
            # 网页返回 answer（Agent 主动呼叫的场景）
            logger.info("收到 answer，设置远端描述")
            if pc:
                answer = RTCSessionDescription(sdp=msg["sdp"], type=msg["type"])
                await pc.setRemoteDescription(answer)

        elif msg_type == "candidate":
            if pc:
                candidate_dict = msg["candidate"]
                candidate = candidate_from_sdp(candidate_dict["candidate"])
                candidate.sdpMid = candidate_dict.get("sdpMid")
                candidate.sdpMLineIndex = candidate_dict.get("sdpMLineIndex")
                await pc.addIceCandidate(candidate)

        elif msg_type == "reject":
            logger.info("对方拒接")
            is_busy = False

        elif msg_type == "hangup":
            logger.info("对方挂断通话")
            if pc:
                await pc.close()
                pc = None
            if digital_human:
                await digital_human.close()
            is_busy = False
            logger.info("通话结束，空闲")

        elif msg_type == "pong":
            continue


def main():
    asyncio.run(agent_loop())


if __name__ == "__main__":
    main()
