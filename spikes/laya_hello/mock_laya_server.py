"""
最小 mock Laya HTTP 服务（仅用于 spike 客户端链路验证）
================================================

不加载任何真实模型，固定返回四信号结构。
单句延迟固定 80ms（模拟 CPU 推理），AGENTS §6 200ms 目标内。

用法：
  python spikes/laya_hello/mock_laya_server.py &
  python spikes/laya_hello/spike_hello.py

不合并主线。
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


PORT = 7001
INFERENCE_LATENCY_MS = 80  # 模拟 CPU 推理延迟，< 200ms 目标

INTENT_MAP = {
    "天气": ("weather", 0.92, False, False),
    "吃": ("medication", 0.88, False, False),
    "药": ("medication", 0.88, False, False),
    "血压": ("health_checkup", 0.90, False, False),
    "量": ("health_checkup", 0.85, False, False),
    "京剧": ("opera", 0.95, False, False),
    "越剧": ("opera", 0.95, False, False),
    "电影": ("movie", 0.93, False, False),
    "大声": ("chat", 0.90, False, False),
    "换": ("chat", 0.90, False, False),
    "你好": ("chat", 0.92, False, False),
    "问候": ("chat", 0.95, False, False),
}


def classify(text: str) -> dict:
    """关键词匹配分类（纯 mock，占位）"""
    intent, conf, sensitive, out_of_domain = "chat", 0.70, False, False
    for kw, (i, c, s, o) in INTENT_MAP.items():
        if kw in text:
            intent, conf, sensitive, out_of_domain = i, c, s, o
            break
    return {
        "intent": intent,
        "is_sensitive": sensitive,
        "confidence": conf,
        "is_out_of_domain": out_of_domain,
    }


class LayaHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/classify":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
            text = payload.get("state", "")
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        # 模拟 CPU 推理延迟
        time.sleep(INFERENCE_LATENCY_MS / 1000)
        result = classify(text)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def log_message(self, format, *args):
        pass  # 静默


if __name__ == "__main__":
    server = HTTPServer(("localhost", PORT), LayaHandler)
    print(f"Mock Laya serving on http://localhost:{PORT}/classify "
          f"(simulated latency={INFERENCE_LATENCY_MS}ms)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
