"""录制中实时提示：规则与时间窗、经真实 WebSocket 推送、未开启/未安装转写时的行为、不写剪辑决策。"""
from types import SimpleNamespace as NS

import pytest

from app.models import EditDecision
from app.services import live_hints, transcription
from tests.test_pipeline import _create_session
from tests.test_recording_ws import ws_client  # noqa: F401


def words(*items):
    return [NS(text=t, start_ms=s, end_ms=s + 200) for t, s in items]


def test_hints_respect_time_window():
    tokens = words(("我们", 1000), ("我们", 1300), ("嗯", 5000), ("今天", 5400), ("就", 9000), ("就是", 9300))
    hints = live_hints.hints_from_words(tokens, since_ms=0, until_ms=8000)
    assert [(h["kind"], h["text"], h["startMs"]) for h in hints] == [("repetition", "我们", 1000), ("filler", "嗯", 5000)]
    later = live_hints.hints_from_words(tokens, since_ms=8000, until_ms=12000)
    assert [(h["kind"], h["text"]) for h in later] == [("stutter", "就")]
    assert later[0]["message"] == "疑似口吃「就」"
    assert live_hints.hints_from_words([], 0, 1000) == []


class FakeEngine:
    def transcribe(self, path, **kwargs):
        assert kwargs["word_timestamps"] is True
        return iter([NS(start=0.1, end=1.5, text="我们我们今天聊", words=[
            NS(start=0.1, end=0.4, word="我们"), NS(start=0.5, end=0.8, word="我们"), NS(start=0.9, end=1.5, word="今天聊")])]), NS(language="zh")


@pytest.fixture()
def fast_hints(monkeypatch):
    monkeypatch.setattr(live_hints, "INTERVAL_S", 0.2)
    monkeypatch.setattr(live_hints, "TAIL_GUARD_MS", 0)
    monkeypatch.setattr(transcription, "dependency_available", lambda: True)
    monkeypatch.setattr(transcription, "get_model", lambda *a: FakeEngine())


def record_until(ws, predicate, limit=200):
    for _ in range(limit):
        message = ws.receive_json()
        if predicate(message):
            return message
    raise AssertionError("expected message never arrived")


def test_hints_are_pushed_while_recording(ws_client, isolated_backend, clean_webm_file, fast_hints):
    session_id = _create_session(isolated_backend)
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        ws.send_json({"type": "control", "action": "start", "settings": {"liveHints": True}})
        ws.send_bytes(clean_webm_file.read_bytes())
        hint = record_until(ws, lambda m: m.get("type") == "live_hint")
        assert hint["kind"] == "repetition" and hint["text"] == "我们" and hint["message"] == "疑似重复「我们」"
        ws.send_json({"type": "control", "action": "stop"})
    with isolated_backend() as db:
        assert db.query(EditDecision).filter_by(session_id=session_id).count() == 0   # 只是提示，不写决策


def test_no_hints_unless_enabled(ws_client, isolated_backend, clean_webm_file, fast_hints, monkeypatch):
    calls = []
    monkeypatch.setattr(live_hints, "run_live_hints", lambda *a: calls.append(a))
    session_id = _create_session(isolated_backend)
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        ws.send_json({"type": "control", "action": "start"})
        ws.receive_json()
        ws.send_json({"type": "control", "action": "stop"})
    assert calls == []


def test_reports_unavailable_without_transcription(ws_client, isolated_backend, clean_webm_file):
    session_id = _create_session(isolated_backend)      # conftest 默认关闭转写依赖
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        ws.send_json({"type": "control", "action": "start", "settings": {"liveHints": True}})
        notice = record_until(ws, lambda m: m.get("type") == "live_hints_unavailable")
        assert "未安装" in notice["message"]
        ws.send_json({"type": "control", "action": "stop"})


def test_busy_model_skips_round(monkeypatch):
    monkeypatch.setattr(transcription, "get_model", lambda *a: FakeEngine())
    assert transcription._model_lock.acquire(blocking=False)
    try:
        import numpy as np
        assert live_hints._transcribe_tail(np.zeros(16000, dtype=np.float32), 0) == []   # 录后转写占用时跳过
    finally:
        transcription._model_lock.release()


def test_capabilities_endpoint(ws_client, monkeypatch):
    assert ws_client.get("/api/capabilities").json() == {"transcription": False, "liveHints": False}
    monkeypatch.setattr(transcription, "dependency_available", lambda: True)
    assert ws_client.get("/api/capabilities").json() == {"transcription": True, "liveHints": True}
