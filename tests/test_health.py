"""1.4 骨架的回归测试：`/health` 可用 + trace_id 三种路径（生成 / 透传 / 拒绝脏值）。

大纲 1.4 验收原文只有 `curl /health` 返回 200 与「日志每行有 trace_id」两条，
前两条用例把这两条钉成回归；后两条防止「透传任意头」变成日志注入入口。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

client = TestClient(create_app())


def test_health_returns_200() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"]
    assert body["version"] == "0.1.0"
    assert body["trace_id"]


def test_trace_id_generated_and_echoed() -> None:
    response = client.get("/health")

    trace_id = response.headers["X-Trace-Id"]
    assert len(trace_id) == 32
    # 响应体与响应头必须是同一个 ID，否则前端上报无法对齐
    assert response.json()["trace_id"] == trace_id


def test_trace_id_passthrough_from_client() -> None:
    response = client.get("/health", headers={"X-Trace-Id": "abcd1234efgh5678"})

    assert response.headers["X-Trace-Id"] == "abcd1234efgh5678"
    assert response.json()["trace_id"] == "abcd1234efgh5678"


@pytest.mark.parametrize(
    "bad_value",
    [
        "short",  # 少于 8 位
        "bad value with spaces",  # 含空格
        "evil\ninjected-header",  # 换行 = 日志注入/头注入
        "x" * 65,  # 超过 64 位
    ],
)
def test_trace_id_rejects_untrusted_values(bad_value: str) -> None:
    response = client.get("/health", headers={"X-Trace-Id": bad_value})

    trace_id = response.headers["X-Trace-Id"]
    assert trace_id != bad_value
    # 被拒绝时重新生成：32 位十六进制
    assert len(trace_id) == 32 and trace_id.isalnum()
