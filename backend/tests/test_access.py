"""局域网配对码：未启用时不校验；请求头/查询参数均可；错误与限流；WebSocket；日志脱敏。"""
import logging

import pytest
from fastapi import WebSocketDisconnect

from app import access
from tests.test_pipeline import _create_session
from tests.test_recording_ws import ws_client  # noqa: F401

CODE = "K7Q2-9XMA"


@pytest.fixture()
def paired(monkeypatch):
    monkeypatch.setenv("CLEANCUT_ACCESS_TOKEN", CODE)
    access.reset_throttle()
    yield
    access.reset_throttle()


def test_disabled_by_default(ws_client):
    assert ws_client.get("/api/sessions").status_code == 200
    assert ws_client.get("/api/auth/check").json() == {"ok": True, "pairing": False}


def test_requires_code_when_enabled(ws_client, paired):
    assert ws_client.get("/api/health").status_code == 200                 # 健康检查放行
    denied = ws_client.get("/api/sessions")
    assert denied.status_code == 401 and denied.json()["code"] == "pairing_required"
    assert ws_client.get("/api/sessions", headers={"X-CleanCut-Token": CODE}).status_code == 200
    assert ws_client.get("/api/sessions", headers={"X-CleanCut-Token": "k7q29xma"}).status_code == 200   # 忽略大小写与连字符
    assert ws_client.get("/api/sessions?token=K7Q2-9XMA").status_code == 200                           # 媒体链接用查询参数
    assert ws_client.get("/api/sessions", headers={"X-CleanCut-Token": "WRONG"}).status_code == 401
    assert ws_client.get("/api/auth/check", headers={"X-CleanCut-Token": CODE}).json() == {"ok": True, "pairing": True}
    # CORS 预检不需要配对码
    preflight = ws_client.options("/api/sessions", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert preflight.status_code == 200


def test_unauthorized_response_keeps_cors_headers(ws_client, paired):
    response = ws_client.get("/api/sessions", headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_lockout_after_repeated_failures(ws_client, paired):
    for _ in range(access.FAIL_LIMIT):
        assert ws_client.get("/api/sessions", headers={"X-CleanCut-Token": "GUESS"}).status_code == 401
    assert ws_client.get("/api/sessions", headers={"X-CleanCut-Token": "GUESS"}).status_code == 429
    assert ws_client.get("/api/sessions", headers={"X-CleanCut-Token": "AGAIN"}).status_code == 429
    # 已配对的设备不受影响
    assert ws_client.get("/api/sessions", headers={"X-CleanCut-Token": CODE}).status_code == 200


def test_websocket_requires_code(ws_client, isolated_backend, paired):
    session_id = _create_session(isolated_backend)
    with pytest.raises(WebSocketDisconnect) as closed:
        with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
            ws.receive_json()
    assert closed.value.code == 4401
    with ws_client.websocket_connect(f"/ws/recording/{session_id}?token={CODE}") as ws:
        ws.send_json({"type": "control", "action": "start"})
        assert ws.receive_json()["status"] == "RECORDING"
        ws.send_json({"type": "control", "action": "stop"})


def test_token_redacted_from_access_log():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1", "GET", "/api/sessions/x/cover?v=1&token=K7Q2-9XMA", "1.1", 200), None)
    access.RedactTokenFilter().filter(record)
    assert "K7Q2" not in record.getMessage() and "token=***" in record.getMessage()
