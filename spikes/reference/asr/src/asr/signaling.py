import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 在线用户
# uid -> {ws, last_ping_time}
online: dict[str, dict] = {}
# 通话忙线标记：uid -> True/False
busy: dict[str, bool] = {}

PING_INTERVAL = 15
PING_TIMEOUT = 40


async def ping_watcher():
    """后台任务：定时清理僵死连接"""
    while True:
        now = time.time()
        remove_list = []
        for uid, info in online.items():
            if now - info["last_ping_time"] > PING_TIMEOUT:
                remove_list.append(uid)
        for uid in remove_list:
            try:
                await online[uid]["ws"].close()
            except Exception:  # noqa: BLE001, S110
                pass
            del online[uid]
            busy.pop(uid, None)
            logger.warning(f"心跳超时，移除用户 {uid}")
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(ping_watcher())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="WebRTC Signaling Server", lifespan=lifespan)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    uid = None
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")
            uid = msg.get("uid") if msg_type == "login" else uid

            if msg_type == "login":
                uid = msg["uid"]
                online[uid] = {"ws": ws, "last_ping_time": time.time()}
                busy[uid] = False
                logger.info(f"上线 {uid} online={len(online)}")
                continue

            if msg_type == "ping":
                if uid in online:
                    online[uid]["last_ping_time"] = time.time()
                    await ws.send_text(json.dumps({"type": "pong"}))
                continue

            target_uid = msg.get("to")
            if not target_uid:
                continue

            # ========== 忙线检测：invite时检查对方是否忙 ==========
            if msg_type == "invite":
                if target_uid not in online:
                    await ws.send_text(
                        json.dumps({"type": "error", "reason": "offline", "to": uid})
                    )
                    continue
                if busy.get(target_uid, False):
                    await ws.send_text(
                        json.dumps({"type": "error", "reason": "busy", "to": uid})
                    )
                    continue

            # 转发消息
            if target_uid in online:
                target_ws = online[target_uid]["ws"]
                await target_ws.send_text(json.dumps(msg))
            else:
                await ws.send_text(
                    json.dumps({"type": "error", "reason": "offline", "to": uid})
                )

    except WebSocketDisconnect:
        logger.info(f"断开 {uid}")
    finally:
        if uid and uid in online:
            del online[uid]
        if uid and uid in busy:
            del busy[uid]


# 静态文件挂载放在最后，避免截胡 /ws 的 WebSocket 请求
app.mount("/", StaticFiles(directory="static", html=True), name="static")


def main():
    """启动信号服务器服务器"""
    import uvicorn

    # 和 webserver.py 共用同一套证书，保持 HTTPS/WSS 协议一致
    uvicorn.run(
        "asr.signaling:app",
        host="0.0.0.0",
        port=8765,
        workers=1,
        ssl_certfile="cert.pem",
        ssl_keyfile="key.pem",
    )
